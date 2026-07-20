"""Read-only online metadata providers."""

from .musicbrainz_provider import MusicBrainzProvider
from .cover_art_archive_provider import CoverArtArchiveProvider

__all__ = ["MusicBrainzProvider", "CoverArtArchiveProvider"]
