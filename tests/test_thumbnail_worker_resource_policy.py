"""Resource-bound behaviour for the thumbnail fallback worker."""

from __future__ import annotations

import warnings

from PIL import Image

from ui.workers import thumbnail_worker as tw


def test_youtube_thumbnail_variants_share_one_cache_entry():
    tw.clear_thumbnail_cache()
    video_id = "6SYvCsbal2o"
    hq = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    maxres = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    payload = b"validated-image-payload"

    tw.store_cached_thumbnail(hq, payload)

    assert tw.get_cached_thumbnail(maxres) == payload
    assert len(tw._THUMBNAIL_CACHE) == 1


def test_fallback_chain_shares_one_timeout_budget(monkeypatch):
    tw.clear_thumbnail_cache()
    # deadline=108; first attempt receives 7s, second 4s, and the third is
    # never started because the total 8-second budget has expired.
    times = iter([100.0, 101.0, 104.0, 108.1])
    monkeypatch.setattr(tw.time, "monotonic", lambda: next(times))

    observed_timeouts = []
    monkeypatch.setattr(
        tw,
        "_download_candidate",
        lambda _url, timeout: observed_timeouts.append(timeout) or None,
    )

    worker = tw.ThumbnailWorker(
        1,
        "https://i.ytimg.com/vi/6SYvCsbal2o/maxresdefault.jpg",
        timeout=8,
    )
    worker.run()

    assert observed_timeouts == [7.0, 4.0]


def test_decompression_bomb_warning_is_rejected(monkeypatch):
    def bomb_open(*_args, **_kwargs):
        warnings.warn("oversized remote image", Image.DecompressionBombWarning)
        raise AssertionError("warning should have been promoted to an exception")

    monkeypatch.setattr(tw.Image, "open", bomb_open)

    assert tw._normalise_image_bytes(b"not-empty") is None
