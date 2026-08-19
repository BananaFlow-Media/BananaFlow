"""
tests/test_thumbnail_worker_fallback.py
=======================================
Validates YouTube thumbnail candidate generation, 404 fallback hierarchy,
and in-memory thumbnail caching in ThumbnailWorker.
"""

import pytest
from unittest.mock import patch, MagicMock
from utils.artwork_cleaner import (
    extract_youtube_video_id,
    get_youtube_thumbnail_candidates,
    clean_artwork_url,
)
from core.playlist_parser import SourcePlatform, classify_url, UrlKind
from ui.workers.thumbnail_worker import (
    ThumbnailWorker,
    get_cached_thumbnail,
    store_cached_thumbnail,
    _THUMBNAIL_CACHE,
    _CACHE_LOCK,
)


def test_extract_youtube_video_id():
    # Various valid YouTube URLs and IDs
    assert extract_youtube_video_id("6SYvCsbal2o") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://www.youtube.com/watch?v=6SYvCsbal2o") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://youtu.be/6SYvCsbal2o?t=10") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://www.youtube.com/shorts/6SYvCsbal2o") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://www.youtube.com/embed/6SYvCsbal2o") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://i.ytimg.com/vi/6SYvCsbal2o/hqdefault.jpg") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://i.ytimg.com/vi/6SYvCsbal2o/maxresdefault.jpg") == "6SYvCsbal2o"
    assert extract_youtube_video_id("https://open.spotify.com/track/12345") == ""
    assert extract_youtube_video_id("") == ""


def test_get_youtube_thumbnail_candidates():
    candidates = get_youtube_thumbnail_candidates("https://www.youtube.com/watch?v=6SYvCsbal2o")
    assert len(candidates) == 5
    assert candidates[0] == "https://i.ytimg.com/vi/6SYvCsbal2o/maxresdefault.jpg"
    assert candidates[1] == "https://i.ytimg.com/vi/6SYvCsbal2o/sddefault.jpg"
    assert candidates[2] == "https://i.ytimg.com/vi/6SYvCsbal2o/hqdefault.jpg"
    assert candidates[3] == "https://i.ytimg.com/vi/6SYvCsbal2o/mqdefault.jpg"
    assert candidates[4] == "https://i.ytimg.com/vi/6SYvCsbal2o/default.jpg"


def test_classify_url_shorts_and_embed():
    p, k = classify_url("https://www.youtube.com/shorts/6SYvCsbal2o")
    assert p == SourcePlatform.YOUTUBE
    assert k == UrlKind.SINGLE_VIDEO

    p2, k2 = classify_url("https://www.youtube.com/embed/6SYvCsbal2o")
    assert p2 == SourcePlatform.YOUTUBE
    assert k2 == UrlKind.SINGLE_VIDEO


def test_thumbnail_worker_fallback_on_404():
    with _CACHE_LOCK:
        _THUMBNAIL_CACHE.clear()

    # Create dummy image bytes (>200 bytes)
    dummy_jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 300

    def fake_requests_get(url, *args, **kwargs):
        mock_resp = MagicMock()
        if "maxresdefault.jpg" in url:
            mock_resp.status_code = 404
            mock_resp.content = b""
        elif "sddefault.jpg" in url:
            mock_resp.status_code = 404
            mock_resp.content = b""
        elif "hqdefault.jpg" in url:
            mock_resp.status_code = 200
            mock_resp.content = dummy_jpeg
        else:
            mock_resp.status_code = 404
            mock_resp.content = b""
        return mock_resp

    with patch("requests.get", side_effect=fake_requests_get):
        worker = ThumbnailWorker(1, "https://i.ytimg.com/vi/testvid1234/maxresdefault.jpg")
        emitted_results = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted_results.append((idx, data)))

        worker.run()

        assert len(emitted_results) == 1
        assert emitted_results[0][0] == 1
        assert emitted_results[0][1] == dummy_jpeg


def test_thumbnail_cache_instant_retrieval():
    with _CACHE_LOCK:
        _THUMBNAIL_CACHE.clear()

    dummy_data = b"CACHE_TEST_IMAGE_BYTES" * 10
    test_url = "https://example.com/cover.jpg"

    store_cached_thumbnail(test_url, dummy_data)
    assert get_cached_thumbnail(test_url) == dummy_data

    # Running worker for cached URL should emit without network request
    with patch("requests.get", side_effect=Exception("Should not be called")):
        worker = ThumbnailWorker(42, test_url)
        emitted = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted.append((idx, data)))
        worker.run()

        assert len(emitted) == 1
        assert emitted[0] == (42, dummy_data)

