"""B9 bounded thumbnail cache and cancellable worker contracts."""
from __future__ import annotations

from ui.workers.artwork_thumbnail_worker import ArtworkThumbnailCache, ArtworkThumbnailWorker
from tests.test_artwork_validation_phase7 import image_bytes


def test_thumbnail_cache_is_lru_bounded_by_entries_and_cost():
    cache = ArtworkThumbnailCache(max_entries=2, max_bytes=5)
    cache.put(("a", 1), b"aa"); cache.put(("b", 1), b"bb")
    assert cache.get(("a", 1)) == b"aa"
    cache.put(("c", 1), b"cc")
    assert cache.get(("b", 1)) is None and cache.get(("a", 1)) == b"aa"
    assert cache.cost <= 5


def test_cancelled_thumbnail_worker_never_emits_a_result():
    worker = ArtworkThumbnailWorker((1, (1,), "hash", 1, "current"), image_bytes())
    received = []; worker.ready.connect(lambda *_: received.append(True))
    worker.cancel(); worker.run()
    assert not received


def test_thumbnail_worker_returns_bounded_png_not_full_source():
    worker = ArtworkThumbnailWorker((1, (1,), "hash", 1, "current"), image_bytes(), 1)
    received = []; worker.ready.connect(lambda _token, data: received.append(data))
    worker.run()
    assert received and len(received[0]) < len(image_bytes()) * 4
