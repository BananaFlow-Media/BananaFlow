"""
utils/network_probe.py  –  Lightweight network reachability probe
=================================================================
Pure stdlib + httpx — zero GUI imports.  Used by core/offline_monitor.py
and can be reused by any other module that needs a connectivity check.
"""

from __future__ import annotations

import httpx

_PROBE_URL     = "https://dns.google"
_PROBE_TIMEOUT = 4.0   # seconds


def probe_network() -> bool:
    """Return True if the internet is reachable, False otherwise."""
    try:
        resp = httpx.head(_PROBE_URL, timeout=_PROBE_TIMEOUT, follow_redirects=True)
        return resp.status_code < 500
    except Exception:
        return False
