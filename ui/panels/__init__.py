"""
ui/panels/__init__.py
=====================
Full-size panel widgets that occupy the main navigation areas of AppWindow.

Each panel is a self-contained QWidget subclass that communicates upward
exclusively through Qt signals.  Panels import from ui/components/ but
nothing in ui/components/ imports from here (strict one-way dependency).

Panels
------
UrlBar        – URL entry, paste, clear, fetch, batch import, clipboard indicator.
SearchPanel   – Universal search UI with platform tabs and incremental results.
QueuePanel    – Drag-and-drop download queue with per-track progress cards.
HistoryPanel  – Scrollable download history with search, export, and actions.
OptionsBar    – Format / quality / codec / output-directory controls.
StatusBar     – Global progress bar, status text, speed, and ETA.
"""

from ui.panels.url_bar       import UrlBar
from ui.panels.search_panel  import SearchPanel
from ui.panels.queue_panel   import QueuePanel
from ui.panels.history_panel import HistoryPanel
from ui.panels.options_bar   import OptionsBar
from ui.panels.status_bar    import StatusBar

__all__ = [
    "UrlBar",
    "SearchPanel",
    "QueuePanel",
    "HistoryPanel",
    "OptionsBar",
    "StatusBar",
]
