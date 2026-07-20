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
