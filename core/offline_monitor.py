"""
core/offline_monitor.py  –  Offline / network detection  (pure Python)
=======================================================================
A headless network monitor that polls reachability in a background thread
and fires Python callbacks on state changes.

Zero GUI imports — this module works in CLI mode and unit tests without Qt.
The Qt-signal wrapper lives in ui/workers/offline_monitor.py.

Usage (headless / CLI)
----------------------
    monitor = NetworkMonitor(on_offline=my_fn, on_online=my_fn)
    monitor.start()
    ...
    monitor.stop()

Usage (GUI — via the Qt wrapper)
---------------------------------
    from ui.workers.offline_monitor import OfflineMonitor
    self._net_monitor = OfflineMonitor(parent=self)
    self._net_monitor.went_offline.connect(...)
    self._net_monitor.start()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from utils.network_probe import probe_network

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 15.0   # seconds between probes
_INITIAL_DELAY = 0.5    # seconds before the first probe after start()


class NetworkMonitor:
    """
    Thread-based network poller.  Fires ``on_offline`` / ``on_online``
    callbacks when connectivity state changes.

    Parameters
    ----------
    on_offline : callable, optional
        Called (no arguments) when the network goes down.
    on_online : callable, optional
        Called (no arguments) when the network comes back.
    poll_interval : float
        Seconds between consecutive probes (default 15).
    """

    def __init__(
        self,
        on_offline:    Optional[Callable[[], None]] = None,
        on_online:     Optional[Callable[[], None]] = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._on_offline    = on_offline
        self._on_online     = on_online
        self._poll_interval = poll_interval

        self._online: Optional[bool] = None   # None = unknown (initial)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background polling thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="NetworkMonitor",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the polling thread to stop (non-blocking)."""
        self._stop_event.set()

    @property
    def is_online(self) -> bool:
        """True if the last probe succeeded (optimistic when unknown)."""
        return self._online is not False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        # Brief initial delay so the app window can finish starting up
        self._stop_event.wait(_INITIAL_DELAY)
        if self._stop_event.is_set():
            return

        while not self._stop_event.is_set():
            self._check()
            self._stop_event.wait(self._poll_interval)

    def _check(self) -> None:
        reachable = probe_network()
        if reachable == self._online:
            return   # no state change

        prev = self._online
        self._online = reachable

        if reachable:
            logger.info("[NetworkMonitor] Network reachable.")
            if prev is False and self._on_online:
                self._on_online()
        else:
            logger.warning("[NetworkMonitor] Network unreachable.")
            if self._on_offline:
                self._on_offline()
