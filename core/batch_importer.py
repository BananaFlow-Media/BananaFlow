"""
core/batch_importer.py  –  Multi-URL batch importer
=====================================================
Responsibilities
----------------
* Parse a plain-text file containing one or more media URLs and return only
  the lines that classify_url() recognises as supported media links.
* Parse a raw multi-line / multi-URL string pasted directly into the URL bar
  (the user may paste a block of text with several URLs mixed with garbage).
* Detect and extract URLs embedded inside arbitrary text (e.g. copied from
  a browser bookmarks export, an email, or a Markdown document).
* Validate every candidate URL through classify_url() so the queue panel
  only ever receives URLs the download engine can actually handle.
* Deduplicate the results while preserving the original discovery order.

Design decisions
----------------
* Zero GUI imports – pure stdlib only (re, pathlib, urllib.parse).
* classify_url() is the single source of truth for what is "supported" –
  BatchImporter never maintains its own platform-detection logic.
* Lines beginning with '#' are treated as comments and skipped, so users
  can annotate their batch files.
* Empty lines, whitespace-only lines, and lines that are clearly not URLs
  (no 'http' prefix) are skipped without raising any error.
* All methods are @staticmethod – no instance state required.  Import and
  call directly without instantiation.

Supported input formats
-----------------------
Text file (.txt)  – one URL per line, '#' for comments:

    # My download batch
    https://www.youtube.com/watch?v=TESTVIDEOAAA
    https://open.spotify.com/album/TESTALBUMID00001
    # this line is a comment and will be skipped
    https://www.youtube.com/playlist?list=PLxxxxxx

Raw pasted text   – URLs may appear anywhere, separated by whitespace,
                    commas, newlines, or angle brackets:

    Check these out:
    https://youtu.be/abc123, https://youtu.be/xyz789
    and also https://open.spotify.com/track/TESTTRACKID00001

Usage
-----
>>> from core.batch_importer import BatchImporter
>>>
>>> # From a file
>>> urls = BatchImporter.from_text_file("/path/to/batch.txt")
>>> print(f"{len(urls)} valid URLs found")
>>>
>>> # From a pasted string
>>> pasted = "https://youtu.be/abc  https://youtu.be/xyz"
>>> urls = BatchImporter.from_raw_text(pasted)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from core.playlist_parser import classify_url, SourcePlatform


# ──────────────────────────────────────────────────────────────────────────────
# URL extraction regex
# ──────────────────────────────────────────────────────────────────────────────

# Broad pattern to pull any http/https URL out of arbitrary text.
# Terminates on whitespace, common punctuation used after URLs in prose,
# and angle/square brackets.  The negative lookbehind avoids consuming
# trailing punctuation that is part of the surrounding sentence.
_URL_RE = re.compile(
    r'https?://'                        # scheme (required)
    r'[^\s<>\'"()[\]{},;]+'            # host + path characters
    r'(?<![.,!?:;\'"])',                # strip trailing punctuation
    re.IGNORECASE,
)

# Lines to skip unconditionally.
_COMMENT_RE  = re.compile(r'^\s*#')          # comment lines
_BLANK_RE    = re.compile(r'^\s*$')          # empty / whitespace-only lines


# ──────────────────────────────────────────────────────────────────────────────
# ImportResult  –  returned by both public methods for rich reporting
# ──────────────────────────────────────────────────────────────────────────────

class ImportResult:
    """
    Container returned by BatchImporter methods.

    Attributes
    ----------
    urls          : Deduplicated list of validated, supported media URLs in
                    discovery order.
    total_lines   : Total non-blank, non-comment lines examined.
    skipped_count : Lines / candidates that were not recognised as supported URLs.
    source_label  : Human-readable description of the source (filename or "pasted text").
    """

    def __init__(
        self,
        urls:          list[str],
        total_lines:   int,
        skipped_count: int,
        source_label:  str,
    ) -> None:
        self.urls          = urls
        self.total_lines   = total_lines
        self.skipped_count = skipped_count
        self.source_label  = source_label

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def found_count(self) -> int:
        return len(self.urls)

    @property
    def youtube_urls(self) -> list[str]:
        return [
            u for u in self.urls
            if classify_url(u)[0] in (SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC)
        ]

    @property
    def spotify_urls(self) -> list[str]:
        return [
            u for u in self.urls
            if classify_url(u)[0] == SourcePlatform.SPOTIFY
        ]

    def summary(self) -> str:
        """Return a one-line human-readable summary for display in the status bar."""
        if self.found_count == 0:
            return (
                f"No supported URLs found in {self.source_label} "
                f"({self.total_lines} line(s) examined)."
            )
        parts = []
        yt  = len(self.youtube_urls)
        sp  = len(self.spotify_urls)
        if yt:
            parts.append(f"{yt} YouTube")
        if sp:
            parts.append(f"{sp} Spotify")
        other = self.found_count - yt - sp
        if other:
            parts.append(f"{other} other")
        platform_summary = " · ".join(parts)
        return (
            f"Imported {self.found_count} URL(s) from {self.source_label}  "
            f"[{platform_summary}]"
            + (f"  ·  {self.skipped_count} skipped" if self.skipped_count else "")
        )

    def __repr__(self) -> str:
        return (
            f"ImportResult(found={self.found_count}, "
            f"skipped={self.skipped_count}, "
            f"source={self.source_label!r})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# BatchImporter
# ──────────────────────────────────────────────────────────────────────────────

class BatchImporter:
    """
    Parse URLs from text files or raw pasted text.

    All methods are static – no instantiation needed:
        result = BatchImporter.from_text_file("/path/to/batch.txt")
        result = BatchImporter.from_raw_text(pasted_string)
    """

    # ── Public API ─────────────────────────────────────────────────────────────

    @staticmethod
    def from_text_file(path: str) -> ImportResult:
        """
        Read a plain-text file and return all supported media URLs found in it.

        Parameters
        ----------
        path : str
            Absolute or relative path to a .txt file (or any text file).
            The file is read as UTF-8; falls back to latin-1 on decode errors.

        Returns
        -------
        ImportResult
            Contains the validated URL list plus metadata for status display.

        Raises
        ------
        FileNotFoundError
            If the path does not exist.
        PermissionError
            If the file cannot be read.
        ValueError
            If the file appears to be binary (non-text).
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Batch file not found: {path}")
        if not file_path.is_file():
            raise ValueError(f"Path is not a regular file: {path}")

        # Read with UTF-8 fallback to latin-1 to handle European filenames, etc.
        try:
            raw_text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                raw_text = file_path.read_text(encoding="latin-1")
            except UnicodeDecodeError:
                raise ValueError(
                    f"File does not appear to be a text file: {path}"
                )

        return BatchImporter._parse_text(
            text=raw_text,
            source_label=file_path.name,
            line_mode=True,
        )

    @staticmethod
    def from_raw_text(text: str) -> ImportResult:
        """
        Extract all supported media URLs from a raw string.

        This is used when the user pastes multiple URLs (or a block of text
        containing URLs) directly into the URL bar.  It handles:
            - One URL per line
            - Multiple URLs on the same line (separated by spaces/commas)
            - URLs embedded in surrounding prose text
            - Mixed YouTube and Spotify URLs in the same block

        Parameters
        ----------
        text : str
            Any string; may contain non-URL content freely.

        Returns
        -------
        ImportResult
            Contains the validated URL list plus metadata.
        """
        if not text or not text.strip():
            return ImportResult(
                urls=[],
                total_lines=0,
                skipped_count=0,
                source_label="pasted text",
            )

        return BatchImporter._parse_text(
            text=text,
            source_label="pasted text",
            line_mode=False,
        )

    @staticmethod
    def from_clipboard_text(text: str) -> list[str]:
        """
        Lightweight variant used by ClipboardWorker.

        Returns a plain list of validated URLs rather than an ImportResult,
        since the clipboard monitor does not need the rich metadata.
        Designed to be called very frequently (every ~800 ms) so it avoids
        any I/O and keeps CPU usage minimal.
        """
        if not text or not text.strip():
            return []
        result = BatchImporter._parse_text(
            text=text,
            source_label="clipboard",
            line_mode=False,
        )
        return result.urls

    # ── Internal parsing pipeline ──────────────────────────────────────────────

    @staticmethod
    def _parse_text(
        text:         str,
        source_label: str,
        line_mode:    bool,
    ) -> ImportResult:
        """
        Core parsing pipeline shared by from_text_file() and from_raw_text().

        Parameters
        ----------
        text        : Raw input string.
        source_label: Human-readable label for the ImportResult.
        line_mode   : If True, each line is treated as a potential single URL
                      (comment/blank filtering applies).  If False, the full
                      regex extractor runs on the entire text blob.
        """
        candidates: list[str] = []
        total_lines:   int    = 0
        skipped_count: int    = 0

        if line_mode:
            lines = text.splitlines()
            for line in lines:
                # Skip blank lines and comment lines without counting them
                if _BLANK_RE.match(line) or _COMMENT_RE.match(line):
                    continue

                total_lines += 1

                # Extract all URLs from this line (handles multiple per line)
                extracted = _URL_RE.findall(line)
                if extracted:
                    candidates.extend(extracted)
                else:
                    skipped_count += 1
        else:
            # Free-form text: extract all URLs regardless of line structure
            candidates = _URL_RE.findall(text)
            total_lines = len(text.splitlines())
            # skipped_count is not meaningful here; leave as 0

        # Validate and deduplicate
        validated = BatchImporter._validate_and_deduplicate(candidates)
        skipped_count += len(candidates) - len(validated)

        return ImportResult(
            urls=validated,
            total_lines=total_lines,
            skipped_count=skipped_count,
            source_label=source_label,
        )

    @staticmethod
    def _validate_and_deduplicate(candidates: list[str]) -> list[str]:
        """
        Filter `candidates` through classify_url() and remove duplicates,
        preserving original discovery order.

        A URL is considered a duplicate if it is identical after stripping
        trailing slashes and normalising the scheme to lowercase.
        """
        seen:      set[str]  = set()
        validated: list[str] = []

        for raw_url in candidates:
            url = raw_url.strip().rstrip("/")
            if not url:
                continue

            # Normalise: lowercase scheme, strip fragment (#...) for dedup key
            try:
                parsed    = urlparse(url)
                dedup_key = parsed._replace(
                    scheme=parsed.scheme.lower(),
                    fragment="",
                ).geturl().rstrip("/")
            except Exception:
                dedup_key = url.lower()

            if dedup_key in seen:
                continue

            # classify_url() is the authority on what is "supported"
            platform, kind = classify_url(url)
            if platform == SourcePlatform.UNKNOWN:
                continue

            seen.add(dedup_key)
            validated.append(url)

        return validated


# ──────────────────────────────────────────────────────────────────────────────
# Smoke-test  (python core/batch_importer.py)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import tempfile
    import os

    print("=" * 64)
    print("BatchImporter  –  smoke-test")
    print("=" * 64)
    print()

    # ── 1. from_text_file ─────────────────────────────────────────────────────
    print("── 1. from_text_file ──")

    batch_content = """\
# Batch download list
# YouTube
https://www.youtube.com/watch?v=EXAMPLE_VIDEO_ID_1
https://youtu.be/EXAMPLE_VIDEO_ID_2

# Playlists
https://www.youtube.com/playlist?list=EXAMPLE_PLAYLIST_ID

# YouTube Music
https://music.youtube.com/playlist?list=EXAMPLE_YTM_PLAYLIST_ID

# Spotify
https://open.spotify.com/track/EXAMPLE_TRACK_ID
https://open.spotify.com/album/EXAMPLE_ALBUM_ID
https://open.spotify.com/playlist/EXAMPLE_PLAYLIST_ID_2

# Blank and invalid lines below – should be skipped

not-a-url
http://
https://notasupportedsite.com/video
"""

    # Write to a temp file
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(batch_content)
        tmp_path = tmp.name

    try:
        result = BatchImporter.from_text_file(tmp_path)
        print(f"  Source: {result.source_label}")
        print(f"  Lines examined: {result.total_lines}")
        print(f"  Valid URLs found: {result.found_count}")
        print(f"  Skipped: {result.skipped_count}")
        print(f"  YouTube: {len(result.youtube_urls)}  |  Spotify: {len(result.spotify_urls)}")
        print(f"  Summary: {result.summary()}")
        print()
        for i, url in enumerate(result.urls, 1):
            plat, kind = classify_url(url)
            print(f"    [{i:>2}]  {plat.name:<14} {kind.name:<14}  {url}")
        print()
        assert result.found_count == 8, f"Expected 8 valid URLs, got {result.found_count}"
        assert len(result.youtube_urls) == 4, f"Expected 4 YouTube URLs, got {len(result.youtube_urls)}"
        assert len(result.spotify_urls) == 3, f"Expected 3 Spotify URLs, got {len(result.spotify_urls)}"
        print("  ✅  from_text_file assertions passed\n")
    finally:
        os.unlink(tmp_path)

    # ── 2. from_raw_text – multi-URL paste ────────────────────────────────────
    print("── 2. from_raw_text (multi-URL paste) ──")

    pasted = (
        "Hey, check these out:\n"
        "https://www.youtube.com/watch?v=TESTVIDEOAAA, "
        "https://youtu.be/TESTVIDEOAAB\n"
        "and also https://open.spotify.com/track/TESTTRACKID00001\n"
        "totally unrelated: https://www.google.com and some garbage text\n"
    )

    result2 = BatchImporter.from_raw_text(pasted)
    print(f"  Found: {result2.found_count} valid URL(s)")
    print(f"  Summary: {result2.summary()}")
    for url in result2.urls:
        print(f"    → {url}")
    assert result2.found_count == 3, f"Expected 3, got {result2.found_count}"
    print("  ✅  from_raw_text assertions passed\n")

    # ── 3. Deduplication ──────────────────────────────────────────────────────
    print("── 3. Deduplication ──")

    dupes = (
        "https://www.youtube.com/watch?v=TESTVIDEOAAA\n"
        "https://www.youtube.com/watch?v=TESTVIDEOAAA\n"    # exact dupe
        "https://www.youtube.com/watch?v=TESTVIDEOAAA/\n"   # trailing slash dupe
        "https://youtu.be/TESTVIDEOAAB\n"                   # different URL, not a dupe
    )
    result3 = BatchImporter.from_raw_text(dupes)
    print(f"  Input had 4 lines, expected 2 unique URLs, got {result3.found_count}")
    assert result3.found_count == 2, f"Expected 2, got {result3.found_count}"
    print("  ✅  Deduplication assertions passed\n")

    # ── 4. Empty and whitespace-only input ────────────────────────────────────
    print("── 4. Empty / whitespace input ──")

    result4 = BatchImporter.from_raw_text("")
    assert result4.found_count == 0
    result5 = BatchImporter.from_raw_text("   \n\n  \t  ")
    assert result5.found_count == 0
    print("  ✅  Empty input handled gracefully (0 results, no exceptions)\n")

    # ── 5. FileNotFoundError ──────────────────────────────────────────────────
    print("── 5. FileNotFoundError ──")
    try:
        BatchImporter.from_text_file("/nonexistent/path/batch.txt")
        print("  ❌  Expected FileNotFoundError was not raised")
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"  ✅  FileNotFoundError raised correctly: {exc}\n")

    # ── 6. from_clipboard_text (lightweight variant) ──────────────────────────
    print("── 6. from_clipboard_text ──")
    clipboard_urls = BatchImporter.from_clipboard_text(
        "https://www.youtube.com/watch?v=TESTVIDEOAAA random stuff"
    )
    assert clipboard_urls == ["https://www.youtube.com/watch?v=TESTVIDEOAAA"]
    print(f"  Found: {clipboard_urls}")
    print("  ✅  from_clipboard_text assertions passed\n")

    # ── 7. ImportResult summary edge cases ────────────────────────────────────
    print("── 7. ImportResult summary edge cases ──")
    empty_result = ImportResult(urls=[], total_lines=5, skipped_count=5, source_label="test.txt")
    print(f"  Empty summary: {empty_result.summary()!r}")
    assert "No supported" in empty_result.summary()
    print("  ✅  Empty ImportResult summary correct\n")

    print("=" * 64)
    print("All smoke-tests passed ✅")
    sys.exit(0)
