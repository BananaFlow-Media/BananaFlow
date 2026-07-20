"""
ui/workers/clipboard_worker.py  –  Clipboard URL monitor
=========================================================
Polls the system clipboard every 800 ms and emits url_detected whenever
the clipboard content changes to a new, supported media URL.

Design note – QObject + QTimer, NOT QThread
--------------------------------------------
Qt's QClipboard is a GUI object that belongs to the main thread.  Accessing
it from a QThread (even read-only) is undefined behaviour in Qt and can cause
silent crashes on some platforms.  The correct pattern is to drive the poll
from a QTimer that lives on the main thread.

ClipboardWorker is therefore a QObject with an internal QTimer.  From the
caller's perspective the interface is identical to a QThread:

    self._cb_worker = ClipboardWorker(parent=self)
    self._cb_worker.url_detected.connect(self._on_clipboard_url)
    self._cb_worker.start()    # begin monitoring
    self._cb_worker.stop()     # pause monitoring
    self._cb_worker.is_active  # True while polling

The worker must be created on the main thread (i.e. inside AppWindow.__init__)
so the QTimer's timeout signal fires on the correct thread.

Signal summary
--------------
url_detected(str)
    Emitted at most once per poll cycle, only when the clipboard changes AND
    the new content is (or contains) a supported media URL.  The emitted string
    is the first validated URL found in the clipboard text.
    Multiple URLs in one clipboard paste each trigger a separate emission.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from core.batch_importer import BatchImporter


class ClipboardWorker(QObject):
    """
    Main-thread clipboard monitor driven by a QTimer.

    Parameters
    ----------
    poll_interval_ms : Milliseconds between clipboard checks.  Default 800.
    parent           : Qt parent object (typically AppWindow).
                       Setting a parent ensures the timer is destroyed with
                       the window and cannot fire after shutdown.
    """

    # ── Signals ───────────────────────────────────────────────────────────────

    url_detected = Signal(str)
    # Emitted for each new supported URL found in the clipboard.
    # Multiple URLs in one paste block each produce a separate emission.

    # ── Class-level constant ───────────────────────────────────────────────────

    DEFAULT_POLL_INTERVAL_MS: int = 800

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def __init__(
        self,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        parent: QObject = None,
    ) -> None:
        super().__init__(parent)
        self._last_text: str = ""

        self._timer = QTimer(self)
        self._timer.setInterval(poll_interval_ms)
        self._timer.setSingleShot(False)
        self._timer.timeout.connect(self._check_clipboard)

    # ── Public interface ───────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Begin monitoring the clipboard.
        Seeds _last_text from the current clipboard so we don't immediately
        fire on a URL that was already there before the app launched.
        """
        self._last_text = self._read_clipboard()
        self._timer.start()

    def stop(self) -> None:
        """Pause clipboard monitoring.  Call start() to resume."""
        self._timer.stop()

    @property
    def is_active(self) -> bool:
        """True while the timer is running."""
        return self._timer.isActive()

    def set_interval(self, ms: int) -> None:
        """Change the poll interval at runtime (takes effect on next tick)."""
        self._timer.setInterval(max(200, ms))

    # ── Internal ──────────────────────────────────────────────────────────────

    def _check_clipboard(self) -> None:
        """
        Called every poll_interval_ms on the main thread.
        Reads the clipboard, compares with the last-seen text, and emits
        url_detected for each new supported URL found.
        """
        text = self._read_clipboard()

        # Guard: skip if unchanged or empty
        if not text or text == self._last_text:
            return

        self._last_text = text

        # BatchImporter.from_clipboard_text is a pure-string operation –
        # no I/O, no network.  Safe to call on the main thread.
        urls = BatchImporter.from_clipboard_text(text)
        for url in urls:
            self.url_detected.emit(url)

    @staticmethod
    def _read_clipboard() -> str:
        """
        Safely read the current clipboard text.
        Returns an empty string if the clipboard is unavailable or contains
        non-text content (images, files, etc.).
        """
        try:
            clipboard = QApplication.clipboard()
            if clipboard is None:
                return ""
            text = clipboard.text()
            return text.strip() if text else ""
        except Exception:  # noqa: BLE001
            return ""
