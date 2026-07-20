"""tests/test_channel_scrape_worker_phase6.py — releases/podcasts routing (issue #28).

Verified live against real channels before writing this test: yt-dlp's flat
listing of a channel's Releases or Podcasts tab returns each item as its own
sub-playlist (a real playlist ID under "id"/"url", e.g. "@Eminem/releases" ->
entries like {"id": "OLAK5uy_...", "url": ".../playlist?list=OLAK5uy_..."}),
exactly like the Playlists tab — not flat video entries. Routing these tab
types through `_scrape_flat_tab` (as before this fix) treated each playlist
ID as if it were a video ID, fabricating broken
`/watch?v=<playlist-id>` URLs instead of expanding to the real videos inside.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from core.channel_tab_discoverer import TabInfo
from ui.workers.channel_scrape_worker import ChannelScrapeWorker


def _tab(tab_type: str, name: str = "tab") -> TabInfo:
    return TabInfo(name=name, url=f"https://www.youtube.com/@demo/{tab_type}",
                    icon="🎵", tab_type=tab_type)


def _worker(tabs: list[TabInfo]) -> ChannelScrapeWorker:
    return ChannelScrapeWorker("https://www.youtube.com/@demo", tabs)


# A release/podcast tab listing: each "entry" is itself a sub-playlist,
# matching the real structure observed against @Eminem/releases and
# @lexfridman/podcasts.
_RELEASE_LISTING = {
    "entries": [
        {"id": "OLAK5uy_album1", "title": "Album One",
         "url": "https://www.youtube.com/playlist?list=OLAK5uy_album1"},
    ],
}
_EXPANDED_PLAYLIST = {
    "entries": [
        {"id": "realvideoid1", "title": "Track 1", "duration": 180},
        {"id": "realvideoid2", "title": "Track 2", "duration": 200},
    ],
}


@pytest.mark.parametrize("tab_type", ["releases", "podcasts", "playlists"])
def test_playlist_like_tabs_expand_sub_playlists_into_real_videos(tab_type):
    """Releases/Podcasts/Playlists must all expand each sub-playlist rather
    than treating its playlist ID as a video ID."""
    worker = _worker([_tab(tab_type)])

    call_urls = []

    def fake_extract_info(self, url, download=False):
        call_urls.append(url)
        if "playlist?list=" in url:
            return _EXPANDED_PLAYLIST
        return _RELEASE_LISTING

    with patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract_info):
        videos = worker._scrape_playlists_tab(_tab(tab_type))

    # The tab listing call, then one expansion call per sub-playlist.
    assert len(call_urls) == 2
    assert "playlist?list=OLAK5uy_album1" in call_urls[1]

    # Real video IDs from inside the expanded playlist -- never the
    # sub-playlist's own ID passed through as a fake video ID.
    assert {v.video_id for v in videos} == {"realvideoid1", "realvideoid2"}
    assert all(v.playlist_name == "Album One" for v in videos)
    assert all("OLAK5uy_album1" not in v.url for v in videos)


def test_releases_and_podcasts_are_routed_through_playlist_expansion_not_flat_scrape():
    """The run() dispatch must send releases/podcasts to the same expansion
    path as playlists, not the flat-video path -- this is the actual bug:
    before the fix, only tab_type == "playlists" took this path."""
    worker = _worker([_tab("releases", name="פריטי תוכן")])

    with patch.object(worker, "_scrape_playlists_tab", return_value=[]) as mock_playlists, \
         patch.object(worker, "_scrape_flat_tab", return_value=[]) as mock_flat:
        worker.run()

    mock_playlists.assert_called_once()
    mock_flat.assert_not_called()


def test_podcasts_tab_is_routed_through_playlist_expansion_not_flat_scrape():
    worker = _worker([_tab("podcasts", name="פודקאסטים")])

    with patch.object(worker, "_scrape_playlists_tab", return_value=[]) as mock_playlists, \
         patch.object(worker, "_scrape_flat_tab", return_value=[]) as mock_flat:
        worker.run()

    mock_playlists.assert_called_once()
    mock_flat.assert_not_called()


def test_ordinary_video_tabs_still_use_the_flat_scrape_path():
    """Regression guard: videos/shorts/streams must NOT be routed through
    playlist expansion -- only playlists/releases/podcasts should be."""
    for tab_type in ("videos", "shorts", "streams"):
        worker = _worker([_tab(tab_type)])
        with patch.object(worker, "_scrape_playlists_tab", return_value=[]) as mock_playlists, \
             patch.object(worker, "_scrape_flat_tab", return_value=[]) as mock_flat:
            worker.run()
        mock_playlists.assert_not_called()
        mock_flat.assert_called_once()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
