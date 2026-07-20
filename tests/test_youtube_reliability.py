"""
tests/test_youtube_reliability.py  –  YouTube URL detection
========================================================================
"""

from __future__ import annotations

import pytest

from core.youtube_reliability import is_youtube_url


class TestIsYoutubeUrl:

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=TESTVIDEOAAA",
        "https://youtube.com/watch?v=TESTVIDEOAAA",
        "https://youtu.be/TESTVIDEOAAA",
        "https://music.youtube.com/watch?v=TESTVIDEOAAA",
        "https://m.youtube.com/watch?v=TESTVIDEOAAA",
        "HTTPS://WWW.YOUTUBE.COM/watch?v=TESTVIDEOAAA",
    ])
    def test_youtube_urls_detected(self, url):
        assert is_youtube_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://open.spotify.com/track/TESTTRACKID00001",
        "https://example.com/some-video",
        "https://vimeo.com/12345",
        "",
    ])
    def test_non_youtube_urls_not_detected(self, url):
        assert is_youtube_url(url) is False

    def test_none_safe(self):
        assert is_youtube_url(None) is False

    @pytest.mark.parametrize("url", [
        # "youtube.com" appears in the string but not as the hostname —
        # a substring check would wrongly treat these as YouTube URLs.
        "https://example.com/?redirect=youtube.com",
        "https://notyoutube.com/watch?v=TESTVIDEOAAA",
        "https://youtube.com.evil.test/watch?v=TESTVIDEOAAA",
        "https://evil.test/youtube.com",
        "https://youtu.be.evil.test/TESTVIDEOAAA",
    ])
    def test_lookalike_domains_rejected(self, url):
        assert is_youtube_url(url) is False
