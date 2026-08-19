"""Regression coverage for YouTube thumbnail fallback and caching."""

from __future__ import annotations

import io
from unittest.mock import patch

from PIL import Image

from core.playlist_parser import SourcePlatform, UrlKind, classify_url
from ui.workers import thumbnail_worker as tw
from utils.artwork_cleaner import (
    extract_youtube_video_id,
    get_youtube_thumbnail_candidates,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes = b"", content_type: str = "image/jpeg", content_length=None):
        self.status_code = status_code
        self._body = body
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        elif body:
            self.headers["Content-Length"] = str(len(body))
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 40, 60)).save(buf, format="JPEG")
    return buf.getvalue()


def test_extract_youtube_video_id_accepts_known_shapes_only():
    video_id = "6SYvCsbal2o"
    assert extract_youtube_video_id(video_id) == video_id
    assert extract_youtube_video_id(f"https://www.youtube.com/watch?v={video_id}") == video_id
    assert extract_youtube_video_id(f"https://youtu.be/{video_id}?t=10") == video_id
    assert extract_youtube_video_id(f"https://www.youtube.com/shorts/{video_id}") == video_id
    assert extract_youtube_video_id(f"https://www.youtube-nocookie.com/embed/{video_id}") == video_id
    assert extract_youtube_video_id(f"https://www.youtube.com/live/{video_id}") == video_id
    assert extract_youtube_video_id(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg") == video_id
    assert extract_youtube_video_id(f"https://i.ytimg.com/vi_webp/{video_id}/maxresdefault.webp") == video_id

    assert extract_youtube_video_id("https://open.spotify.com/track/12345") == ""
    assert extract_youtube_video_id(f"https://example.com/watch?v={video_id}") == ""
    assert extract_youtube_video_id(f"https://example.com/youtu.be/{video_id}") == ""
    assert extract_youtube_video_id("") == ""


def test_get_youtube_thumbnail_candidates_for_video_url():
    video_id = "6SYvCsbal2o"
    candidates = get_youtube_thumbnail_candidates(f"https://www.youtube.com/watch?v={video_id}")
    assert candidates == [
        f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/default.jpg",
    ]


def test_existing_ytimg_variant_is_preserved_first():
    video_id = "6SYvCsbal2o"
    original = f"https://i.ytimg.com/vi/{video_id}/hq720.jpg?custom=1"
    candidates = get_youtube_thumbnail_candidates(original)
    assert candidates[0] == original
    assert len(candidates) == len(set(candidates))


def test_classify_url_shorts_embed_and_live():
    for path in ("shorts", "embed", "live"):
        platform, kind = classify_url(f"https://www.youtube.com/{path}/6SYvCsbal2o")
        assert platform == SourcePlatform.YOUTUBE
        assert kind == UrlKind.SINGLE_VIDEO


def test_thumbnail_worker_falls_back_after_404s():
    tw.clear_thumbnail_cache()
    jpeg = _jpeg_bytes()

    def fake_get(url, **kwargs):
        if "maxresdefault.jpg" in url or "sddefault.jpg" in url:
            return _FakeResponse(404)
        if "hqdefault.jpg" in url:
            return _FakeResponse(200, jpeg)
        return _FakeResponse(404)

    with patch("requests.get", side_effect=fake_get):
        worker = tw.ThumbnailWorker(1, "https://i.ytimg.com/vi/testvid1234/maxresdefault.jpg")
        emitted = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted.append((idx, data)))
        worker.run()

    assert emitted == [(1, jpeg)]


def test_thumbnail_cache_hit_skips_network():
    tw.clear_thumbnail_cache()
    jpeg = _jpeg_bytes()
    url = "https://example.com/cover.jpg"
    tw.store_cached_thumbnail(url, jpeg)

    with patch("requests.get", side_effect=AssertionError("network should not be used")):
        worker = tw.ThumbnailWorker(42, url)
        emitted = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted.append((idx, data)))
        worker.run()

    assert emitted == [(42, jpeg)]


def test_http_200_html_is_not_cached_or_emitted():
    tw.clear_thumbnail_cache()
    url = "https://example.com/cover.jpg"
    html = b"<html><body>temporary CDN error</body></html>" * 20

    with patch("requests.get", return_value=_FakeResponse(200, html, "text/html")):
        worker = tw.ThumbnailWorker(3, url)
        emitted = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted.append((idx, data)))
        worker.run()

    assert emitted == []
    assert tw.get_cached_thumbnail(url) is None


def test_invalid_image_bytes_are_not_emitted_even_with_image_content_type():
    tw.clear_thumbnail_cache()
    url = "https://example.com/cover.jpg"
    junk = b"not actually an image" * 100

    with patch("requests.get", return_value=_FakeResponse(200, junk, "image/jpeg")):
        worker = tw.ThumbnailWorker(4, url)
        emitted = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted.append((idx, data)))
        worker.run()

    assert emitted == []


def test_thumbnail_requests_keep_tls_verification_enabled():
    tw.clear_thumbnail_cache()
    jpeg = _jpeg_bytes()
    url = "https://example.com/cover.jpg"

    with patch("requests.get", return_value=_FakeResponse(200, jpeg)) as get:
        worker = tw.ThumbnailWorker(5, url)
        worker.run()

    kwargs = get.call_args.kwargs
    assert kwargs.get("verify", True) is not False
    assert kwargs["stream"] is True


def test_declared_oversized_response_is_rejected_without_reading_body():
    tw.clear_thumbnail_cache()
    url = "https://example.com/huge.jpg"
    response = _FakeResponse(
        200,
        _jpeg_bytes(),
        content_length=tw._MAX_RESPONSE_BYTES + 1,
    )

    with patch("requests.get", return_value=response):
        worker = tw.ThumbnailWorker(6, url)
        emitted = []
        worker.thumbnail_ready.connect(lambda idx, data: emitted.append((idx, data)))
        worker.run()

    assert emitted == []
    assert response.closed is True


def test_cache_is_bounded_by_bytes_not_only_item_count(monkeypatch):
    tw.clear_thumbnail_cache()
    monkeypatch.setattr(tw, "_CACHE_MAX_BYTES", 12)
    monkeypatch.setattr(tw, "_CACHE_MAX_ITEMS", 100)

    tw.store_cached_thumbnail("a", b"12345678")
    tw.store_cached_thumbnail("b", b"abcdefgh")

    assert tw.get_cached_thumbnail("a") is None
    assert tw.get_cached_thumbnail("b") == b"abcdefgh"
    assert tw._THUMBNAIL_CACHE_BYTES <= 12
