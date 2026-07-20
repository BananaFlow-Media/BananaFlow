"""
utils/logger.py  –  Shared logging utilities for BananaFlow
===================================================================
Provides a single SilentLogger class used by yt-dlp across all backend
modules (downloader, playlist_parser, search_engine) to suppress console
output while still capturing warnings and errors for programmatic inspection.

Design
------
* Zero GUI imports – pure stdlib only.
* The class implements the yt-dlp logger interface (debug / info / warning /
  error) so it can be passed directly as ``opts["logger"] = SilentLogger()``.
* warnings and errors are accumulated in lists so callers can inspect them
  after a yt-dlp operation completes.

Previously this class was duplicated as ``_SilentLogger`` in both
``core/search_engine.py`` and ``playlist_parser.py``.  All references have
been updated to import from here.
"""

from __future__ import annotations


class SilentLogger:
    """
    yt-dlp-compatible logger that swallows all stdout/stderr noise while
    retaining warnings and errors in lists for post-hoc inspection.

    Usage
    -----
    >>> logger = SilentLogger()
    >>> ydl_opts = {"logger": logger, "quiet": True}
    >>> with yt_dlp.YoutubeDL(ydl_opts) as ydl:
    ...     ydl.extract_info(url, download=False)
    >>> if logger.errors:
    ...     print("Errors:", logger.errors)
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors:   list[str] = []

    # ── yt-dlp logger interface ────────────────────────────────────────────────

    def debug(self, msg: str) -> None:
        """Suppress debug output entirely."""

    def info(self, msg: str) -> None:
        """Suppress informational output entirely."""

    def warning(self, msg: str) -> None:
        """Accumulate warnings for caller inspection."""
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        """Accumulate errors for caller inspection."""
        self.errors.append(msg)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def has_errors(self) -> bool:
        return bool(self.errors)

    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def clear(self) -> None:
        """Reset accumulated messages (call before reusing the logger instance)."""
        self.warnings.clear()
        self.errors.clear()

    def __repr__(self) -> str:
        return (
            f"SilentLogger(warnings={len(self.warnings)}, errors={len(self.errors)})"
        )
