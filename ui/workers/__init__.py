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

from ui.workers.fetch_worker     import FetchWorker
from ui.workers.download_worker  import DownloadWorker
from ui.workers.thumbnail_worker import ThumbnailWorker
from ui.workers.clipboard_worker import ClipboardWorker
from ui.workers.search_worker    import SearchWorker
from ui.workers.scraper_worker   import ScraperWorker
from ui.workers.update_worker    import UpdateWorker, UpdateCheckResults
from ui.workers.component_install_worker import ComponentInstallWorker

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
