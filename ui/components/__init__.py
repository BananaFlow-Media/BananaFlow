"""
ui/components/__init__.py
=========================
Reusable leaf-level widgets for BananaFlow.

Each component is a self-contained QWidget subclass that owns its own
layout, signals, and styling.  Panels import from here; nothing in this
package imports from ui/panels/ (strict one-way dependency).

Components
----------
TrackCard          – Draggable queue entry with thumbnail, progress, status.
SearchResultCard   – Compact search result with "Add to Queue" action.
HistoryRow         – One row in the download-history table.
"""

from ui.components.track_card          import TrackCard
from ui.components.search_result_card  import SearchResultCard
from ui.components.history_row         import HistoryRow

__all__ = [
    "TrackCard",
    "SearchResultCard",
    "HistoryRow",
]
