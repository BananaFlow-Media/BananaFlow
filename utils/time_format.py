"""
utils/time_format.py  –  Shared duration formatting helper
===========================================================
Single source of truth for converting a raw number of seconds into the
human-readable "M:SS" / "H:MM:SS" strings displayed throughout the app.

Previously there were three separate copies of this logic spread across
playlist_parser.py, core/search_engine.py, and core/history_db.py.
"""

from __future__ import annotations

import datetime as _datetime
from typing import Optional


def seconds_to_str(seconds: Optional[int | float], *, live_label: str = "") -> str:
    """
    Convert *seconds* to a compact time string.

    Parameters
    ----------
    seconds :
        Duration in seconds.  ``None`` or negative values return *live_label*.
    live_label :
        String returned when *seconds* is ``None`` (default ``""``).
        Pass ``"Live"`` for playlist cards or ``"—"`` for history rows.

    Returns
    -------
    str
        ``"M:SS"`` for durations under one hour, ``"H:MM:SS"`` otherwise,
        or *live_label* when the duration is unknown.

    Examples
    --------
    >>> seconds_to_str(65)
    '1:05'
    >>> seconds_to_str(3661)
    '1:01:01'
    >>> seconds_to_str(None, live_label="Live")
    'Live'
    """
    if seconds is None:
        return live_label
    s = int(seconds)
    if s < 0:
        return live_label
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def _as_datetime(when: _datetime.datetime | float | int) -> _datetime.datetime:
    """A ``datetime`` is used as-is, in whatever timezone it carries; a Unix
    timestamp is converted via ``fromtimestamp`` (local time), matching every
    call site's previous behaviour."""
    if isinstance(when, _datetime.datetime):
        return when
    return _datetime.datetime.fromtimestamp(when)


def timestamp_to_str(when: _datetime.datetime | float | int) -> str:
    """
    Format a timestamp for a **technical** context as ``"YYYY-MM-DD HH:MM"``.

    ISO-style ordering, deliberately locale-independent. Use this for log
    lines, filenames, machine-readable output, exported evidence, and
    anything else read by a tool or by a developer diagnosing a problem —
    contexts where sorting correctly and never being ambiguous matter more
    than matching the reader's local convention.

    **Do not use this for text shown to a user**: call
    :func:`display_timestamp` instead, which follows the active UI language
    (issue #43).
    """
    return _as_datetime(when).strftime("%Y-%m-%d %H:%M")


def display_timestamp(when: _datetime.datetime | float | int) -> str:
    """
    Format a timestamp the way the **user's** language writes dates.

    Hebrew (like most of the world outside the ISO/US conventions) writes
    day-first: ``05/03/2026 14:30``. English keeps the unambiguous ISO
    ordering ``2026-03-05 14:30``. The time part is 24-hour in both, which
    is what the app has always shown and what both audiences read here.

    Issue #43 asked whether dates "follow locale conventions consistently
    across all panels". Showing an Israeli user ``2026-03-05`` is
    consistent, but it is consistently *foreign*: DD/MM is what they write.
    The technical contexts that genuinely need sortable, unambiguous
    timestamps keep :func:`timestamp_to_str`.

    The returned string is always left-to-right. Callers embedding it in
    RTL prose must still wrap it — see ``ui.direction.isolate_number`` — or
    Unicode's bidi algorithm will reorder its parts.
    """
    dt = _as_datetime(when)
    # Imported lazily and defensively: utils/ must stay importable headlessly
    # (CONTRIBUTING, "Core/UI separation"), and ui.i18n is the one documented
    # plain-Python exception backend modules may read. A CLI or test process
    # that never set a language falls back to the technical format.
    try:
        from ui.i18n import current_language
        language = current_language()
    except Exception:  # noqa: BLE001 - never let formatting break on i18n state
        language = "en"

    if language == "he":
        return dt.strftime("%d/%m/%Y %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")
