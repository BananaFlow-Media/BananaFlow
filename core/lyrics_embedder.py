"""
core/lyrics_embedder.py  –  Lyrics fetch + embed (Advanced Setting)
====================================================================
Fetches plain-text lyrics for a track using the syncedlyrics library
(which queries multiple lyrics sources in order) and embeds them into
the file's metadata tags.

Supported containers
--------------------
  MP3  → ID3  USLT (Unsynchronised Lyrics) tag
  M4A  → iTunes ©lyr atom via mutagen.mp4
  FLAC → LYRICS Vorbis comment via mutagen.flac
  OPUS → LYRICS Vorbis comment via mutagen.oggvorbis

This module is **disabled by default** and is only called from
downloader.py when AppConfig.lyrics_enabled is True.

Zero GUI imports.  Raises LyricsError on unrecoverable failure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class LyricsError(Exception):
    """Raised when lyrics cannot be fetched or embedded."""


def _fetch_lyrics_genius_fallback(title: str, artist: str) -> Optional[str]:
    """
    Fallback lyrics search on genius.com using the public multi-search API
    and BeautifulSoup parsing (no API key required).
    """
    import urllib.parse
    import re
    import requests
    from bs4 import BeautifulSoup

    query = f"{artist} {title}".strip()
    url = f"https://genius.com/api/search/multi?q={urllib.parse.quote(query)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None

        data = response.json()
        sections = data.get("response", {}).get("sections", [])
        for section in sections:
            type_ = section.get("type")
            if type_ not in ("top_hit", "song"):
                continue
            hits = section.get("hits", [])
            for hit in hits:
                result = hit.get("result", {})
                song_url = result.get("url")
                # Make sure it's a lyrics page (url contains "-lyrics")
                if song_url and "-lyrics" in song_url:
                    lyrics_resp = requests.get(song_url, headers=headers, timeout=10)
                    if lyrics_resp.status_code == 200:
                        lyrics_soup = BeautifulSoup(lyrics_resp.text, "html.parser")
                        containers = lyrics_soup.find_all("div", {"data-lyrics-container": "true"})
                        if containers:
                            lyrics_text = []
                            for c in containers:
                                for br in c.find_all("br"):
                                    br.replace_with("\n")
                                lyrics_text.append(c.get_text())
                            full_lyrics = "\n".join(lyrics_text).strip()
                            if full_lyrics:
                                # Clean up "Contributors...Lyrics" prefix
                                full_lyrics = re.sub(
                                    r"^\d+\s*Contributors.*?Lyrics",
                                    "",
                                    full_lyrics,
                                    flags=re.DOTALL | re.IGNORECASE
                                ).strip()
                                # Clean up "Read More" annotation block if near the start
                                if "read more" in full_lyrics[:500].lower():
                                    full_lyrics = re.sub(
                                        r"^.*?Read More\s*",
                                        "",
                                        full_lyrics,
                                        flags=re.DOTALL | re.IGNORECASE
                                    ).strip()
                                # Clean up trailing "Embed" suffix
                                full_lyrics = re.sub(
                                    r"\d*Embed$",
                                    "",
                                    full_lyrics,
                                    flags=re.IGNORECASE
                                ).strip()
                                return full_lyrics
    except Exception as exc:
        logger.debug("[LyricsEmbedder] Genius fallback error: %s", exc)
    return None


def _fetch_lyrics(title: str, artist: str) -> Optional[str]:
    """
    Try to fetch plain-text lyrics using syncedlyrics, and fallback to Genius.

    Returns the lyrics string, or None if not found.
    """
    lyrics = None
    try:
        import syncedlyrics  # type: ignore[import]
        query = f"{artist} {title}".strip()
        # syncedlyrics.search returns synced LRC text or falls back to plain
        lyrics = syncedlyrics.search(query, plain_only=True)
        if not lyrics:
            lyrics = syncedlyrics.search(title, plain_only=True)
    except Exception as exc:
        logger.debug("[LyricsEmbedder] syncedlyrics search error: %s", exc)

    if lyrics:
        return lyrics

    # Fallback to Genius public search API
    logger.info("[LyricsEmbedder] Falling back to Genius search for '%s – %s'", artist, title)
    return _fetch_lyrics_genius_fallback(title, artist)


def embed_lyrics(
    file_path: str,
    title:     str,
    artist:    str,
) -> bool:
    """
    Fetch lyrics for the given track and embed them into file_path.

    Parameters
    ----------
    file_path : Absolute path to the downloaded audio file.
    title     : Track title (used for lyrics query).
    artist    : Artist name (used for lyrics query).

    Returns
    -------
    True if lyrics were embedded, False if not found or embedding failed.

    Raises
    ------
    LyricsError if mutagen is not available.
    """
    try:
        import mutagen  # noqa: F401
    except ImportError as exc:
        raise LyricsError("mutagen not installed. Run: pip install mutagen") from exc

    lyrics = _fetch_lyrics(title, artist)
    if not lyrics:
        logger.info("[LyricsEmbedder] No lyrics found for '%s – %s'", artist, title)
        return False

    path = Path(file_path)
    try:
        from core.metadata_backend import METADATA_BACKEND
        METADATA_BACKEND.write_auxiliary(path, "lyrics", {"lyrics": lyrics})
    except Exception as exc:
        logger.error("[LyricsEmbedder] Failed to embed lyrics in %s: %s", file_path, exc)
        return False

    logger.info("[LyricsEmbedder] Lyrics embedded in %s", path.name)
    return True
