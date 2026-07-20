"""Bounded, cancellable artwork thumbnail decoding for the Tag Editor."""
from __future__ import annotations

from collections import OrderedDict
import threading

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QThread, Signal, Qt
from PySide6.QtGui import QImage


class ArtworkThumbnailCache:
    """LRU cache of encoded small thumbnails, never full-resolution images."""
    def __init__(self, max_entries: int = 64, max_bytes: int = 4 * 1024 * 1024) -> None:
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._items: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._cost = 0

    def get(self, key: tuple[str, int]) -> bytes | None:
        data = self._items.get(key)
        if data is not None: self._items.move_to_end(key)
        return data

    def put(self, key: tuple[str, int], data: bytes) -> None:
        data = bytes(data)
        prior = self._items.pop(key, None)
        if prior is not None: self._cost -= len(prior)
        self._items[key] = data; self._cost += len(data)
        while self._items and (len(self._items) > self.max_entries or self._cost > self.max_bytes):
            _, stale = self._items.popitem(last=False); self._cost -= len(stale)

    def clear(self) -> None:
        self._items.clear(); self._cost = 0

    @property
    def cost(self) -> int: return self._cost


class ArtworkThumbnailWorker(QThread):
    """Decode immutable bytes off the GUI thread and return a tiny PNG."""
    ready = Signal(object, bytes)  # token, encoded thumbnail
    failed = Signal(object, str)

    def __init__(self, token: object, data: bytes, size: int = 146, parent=None) -> None:
        super().__init__(parent)
        self._token, self._data, self._size = token, bytes(data), size
        self._cancel = threading.Event()

    def cancel(self) -> None: self._cancel.set()

    def run(self) -> None:
        if self._cancel.is_set(): return
        image = QImage.fromData(self._data)
        if self._cancel.is_set(): return
        if image.isNull(): self.failed.emit(self._token, "meta_artwork_invalid_image"); return
        thumb = image.scaled(self._size, self._size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        if self._cancel.is_set(): return
        payload = QByteArray(); buffer = QBuffer(payload); buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not thumb.save(buffer, "PNG"):
            self.failed.emit(self._token, "meta_artwork_invalid_image"); return
        self.ready.emit(self._token, bytes(payload))
