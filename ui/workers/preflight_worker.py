"""
ui/workers/preflight_worker.py  –  Startup checks, off the GUI thread
======================================================================
``error_handler.run_preflight`` probes FFmpeg, network reachability, the
output directory, the cookies file, and Playwright.  Two of those are
slow by nature:

  * the network probe waits on a real connection attempt;
  * the Playwright check starts Playwright's Node driver process and
    waits for it to answer.

Running them inline during startup blocked the main window *after* it had
already been shown but *before* ``QApplication.exec()`` began, so the
window was on screen and completely unresponsive for the duration —
Windows renders that as a frozen, white, "Not Responding" window.
Measured on the 1.0.0 Windows build: roughly nine seconds.

This worker moves the probing to a background thread so the window is
interactive immediately and the warning dialog (if any) appears when the
answer is actually known.

``run_preflight`` never raises — every failure is captured in its result
object — so ``run()`` needs no try/except.

Signal summary
--------------
completed(object)
    Emitted exactly once with the ``PreflightResult``.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal


class PreflightWorker(QThread):
    """One-shot background preflight runner. Create, connect, start, discard."""

    completed = Signal(object)

    def __init__(self, output_dir: str = "", cookies_file: str = "", parent=None) -> None:
        super().__init__(parent)
        self._output_dir = output_dir
        self._cookies_file = cookies_file

    def run(self) -> None:
        from error_handler import run_preflight

        result = run_preflight(
            output_dir=self._output_dir,
            cookies_file=self._cookies_file,
        )
        self.completed.emit(result)
