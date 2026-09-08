"""
ui/workers/__init__.py
======================
Qt worker thread package for BananaFlow.

Each module in this package contains a single QThread (or QObject) subclass
that wraps one backend operation and exposes its results via Qt signals,
keeping all blocking I/O off the main UI thread.

Workers
-------
FetchWorker      – Metadata extraction via PlaylistParser
DownloadWorker   – Media download via DownloadEngine
ThumbnailWorker  – Remote thumbnail image fetching
ClipboardWorker  – Clipboard polling for auto-URL detection  (QObject/QTimer)
SearchWorker     – Universal search via SearchEngine
ScraperWorker    – Deep page scraping via PageScraper
UpdateWorker     – App-release (GitHub) + component (PyPI) update check
ComponentInstallWorker – User-approved in-place pip upgrade of components
"""

from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "FetchWorker": "ui.workers.fetch_worker",
    "DownloadWorker": "ui.workers.download_worker",
    "ThumbnailWorker": "ui.workers.thumbnail_worker",
    "ClipboardWorker": "ui.workers.clipboard_worker",
    "SearchWorker": "ui.workers.search_worker",
    "ScraperWorker": "ui.workers.scraper_worker",
    "UpdateWorker": "ui.workers.update_worker",
    "UpdateCheckResults": "ui.workers.update_worker",
    "ComponentInstallWorker": "ui.workers.component_install_worker",
}


def __getattr__(name: str):
    """Preserve package-level imports without importing every worker at once."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value

__all__ = [
    "FetchWorker",
    "DownloadWorker",
    "ThumbnailWorker",
    "ClipboardWorker",
    "SearchWorker",
    "ScraperWorker",
    "UpdateWorker",
    "UpdateCheckResults",
    "ComponentInstallWorker",
]
