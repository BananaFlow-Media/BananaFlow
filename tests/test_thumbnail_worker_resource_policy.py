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


def test_worker_prefers_extractor_url_before_quality_fallbacks():
    video_id = "6SYvCsbal2o"
    hq = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    candidates = tw._candidate_order(hq)

    assert candidates[0] == hq
    assert candidates[1] == f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    assert len(candidates) == len(set(candidates))


def test_fallback_chain_shares_one_absolute_deadline(monkeypatch):
    tw.clear_thumbnail_cache()
    times = iter([100.0, 101.0, 104.0, 108.1])
    monkeypatch.setattr(tw.time, "monotonic", lambda: next(times))

    observed_deadlines = []
    monkeypatch.setattr(
        tw,
        "_download_candidate",
        lambda _url, deadline: observed_deadlines.append(deadline) or None,
    )

    worker = tw.ThumbnailWorker(
        1,
        "https://i.ytimg.com/vi/6SYvCsbal2o/maxresdefault.jpg",
        timeout=8,
    )
    worker.run()

    assert observed_deadlines == [108.0, 108.0]


def test_fetch_slot_wait_uses_remaining_total_budget(monkeypatch):
    class NoSlot:
        def __init__(self):
            self.timeout = None

        def acquire(self, timeout):
            self.timeout = timeout
            return False

        def release(self):
            raise AssertionError("unacquired slot must not be released")

    slot = NoSlot()
    monkeypatch.setattr(tw, "_FETCH_SLOTS", slot)
    monkeypatch.setattr(tw.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        tw.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network must not start")),
    )

    assert tw._download_candidate("https://example.com/x.jpg", 103.5) is None
    assert slot.timeout == 3.5


def test_decompression_bomb_warning_is_rejected(monkeypatch):
    def bomb_open(*_args, **_kwargs):
        warnings.warn("oversized remote image", Image.DecompressionBombWarning)
        raise AssertionError("warning should have been promoted to an exception")

    monkeypatch.setattr(tw.Image, "open", bomb_open)

    assert tw._normalise_image_bytes(b"not-empty") is None
