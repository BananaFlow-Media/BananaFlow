"""
ui/workers/offline_monitor.py  –  Qt-signal wrapper for NetworkMonitor
=======================================================================
Wraps the headless core.NetworkMonitor in a QObject so that AppWindow
can connect Qt signals to UI widgets (OfflineBanner, etc.).

Import this module from the UI layer only.  Core and CLI code should
use core.offline_monitor.NetworkMonitor directly.

Usage (in AppWindow.__init__)
------------------------------
    from ui.workers.offline_monitor import OfflineMonitor

    self._net_monitor = OfflineMonitor(parent=self)
    self._net_monitor.went_offline.connect(self._offline_banner.show)
    self._net_monitor.came_online.connect(self._offline_banner.hide)
    self._net_monitor.start()
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.offline_monitor import NetworkMonitor


class OfflineMonitor(QObject):
    """
    Qt-aware network monitor.

    Signals
    -------
    went_offline()  – emitted the first time a probe fails after success.
    came_online()   – emitted the first time a probe succeeds after failure.
    """

    went_offline = Signal()
    came_online  = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._monitor = NetworkMonitor(
            on_offline=self._emit_offline,
            on_online=self._emit_online,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background polling thread."""
        self._monitor.start()

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._monitor.stop()

    @property
    def is_online(self) -> bool:
        """True if the last probe succeeded (optimistic when unknown)."""
        return self._monitor.is_online

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit_offline(self) -> None:
        self.went_offline.emit()

    def _emit_online(self) -> None:
        self.came_online.emit()
