"""utils/playwright_check.py — single-source Playwright availability check.

Playwright is an optional runtime dependency. It powers:
  * core/scraper.py             — channel + artist discography scraping
  * core/cookie_wizard.py       — sign-in / cookie-grab browser
  * core/universal_extractor.py — generic-site stream interception
  * core/channel_tab_discoverer.py
  * core/listing_scraper.py

Official packaged builds (Windows EXE, macOS .app) bundle the Chromium
browser (~300 MB): the release workflow installs it before PyInstaller
runs, packaging/bananaflow.spec embeds it, and main.py/cli.py point
PLAYWRIGHT_BROWSERS_PATH inside the bundle. ``scripts/install_playwright.ps1``
is the *source-install* path (it needs a Python on PATH, which a frozen
EXE does not provide). Anything that needs Playwright must call
``require_playwright_or_raise(feature_name)`` before doing work, or
check ``is_playwright_available()`` and degrade gracefully.

Zero GUI imports.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class PlaywrightNotAvailable(RuntimeError):
    """Raised when a feature needs Playwright but it is not installed.

    Carries a user-facing message in both English and Hebrew that
    UI code can surface directly in a MessageBox; the orchestrator
    and CLI use ``str(exc)`` for the English form.
    """

    def __init__(self, feature: str) -> None:
        self.feature = feature
        self.message_en = (
            f"{feature} requires Playwright Chromium, which is not installed.\n\n"
            "Run the following from the BananaFlow install folder to enable it:\n"
            "    scripts/install_playwright.ps1\n\n"
            "Or from a Python install:\n"
            "    python -m playwright install chromium\n\n"
            "All other features continue to work normally."
        )
        self.message_he = (
            f"הפיצ'ר \"{feature}\" דורש את Playwright Chromium שאינו מותקן.\n\n"
            "להפעלה, הרץ מתוך תיקיית ההתקנה של BananaFlow:\n"
            "    scripts/install_playwright.ps1\n\n"
            "או מתוך התקנת Python:\n"
            "    python -m playwright install chromium\n\n"
            "שאר הפעולות בתוכנה ימשיכו לעבוד כרגיל."
        )
        super().__init__(self.message_en)


def is_playwright_available() -> bool:
    """Return True iff the playwright package AND a browser are installed.

    The package alone is not enough: ``pip install playwright`` does
    not download Chromium. We need both the Python bindings and the
    browser binary on disk.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            return bool(exe) and Path(exe).exists()
    except Exception as exc:
        logger.debug("Playwright probe failed: %s", exc)
        return False


def require_playwright_or_raise(feature: str) -> None:
    """Raise ``PlaywrightNotAvailable`` if Playwright cannot be used.

    Call at the top of any function that drives a browser session, so
    the caller can render a friendly error before the deep import of
    ``playwright.sync_api`` raises a less-friendly ImportError.
    """
    if not is_playwright_available():
        raise PlaywrightNotAvailable(feature)
