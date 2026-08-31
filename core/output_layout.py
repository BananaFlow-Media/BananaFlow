"""Provider-neutral output-folder and filename-body policy.

The route used to discover an item must not accidentally change its physical
destination.  In particular, a playlist's collection title is distinct from
the album tag carried by each member track.

This module contains no UI or Qt imports.  Callers provide the category and
disc-label localizers when rendering a folder path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


_SINGLES_CATEGORIES = {
    "סינגלים ו-ep", "סינגלים וגרסאות ep", "סינגלים ומיני אלבומים",
}

_ENGLISH_CATEGORIES = {
    "אלבומים": "Albums",
    "סינגלים ו-EP": "Singles & EPs",
    "סינגלים ומיני אלבומים": "Singles & EPs",
    "פלייליסטים": "Playlists",
    "סרטונים": "Videos",
    "הופעות חיות": "Live Performances",
    "אוספים": "Compilations",
    "מופיע באוספים": "Appears On",
}


def english_category_name(category: str) -> str:
    """Return the stable English CLI label for a canonical category."""

    return _ENGLISH_CATEGORIES.get(category, category)


def _text(value: object) -> str:
    return str(value or "").strip()


def _kind_name(value: object) -> str:
    return _text(getattr(value, "name", value)).upper()


def _dedupe(parts: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for part in parts:
        cleaned = _text(part)
        if cleaned and (not result or cleaned.casefold() != result[-1].casefold()):
            result.append(cleaned)
    return tuple(result)


@dataclass(frozen=True)
class OutputLayoutDecision:
    """Semantic output location plus the safe filename-body choice."""

    artist_folder: str = ""
    category_folder: str = ""
    collection_folder: str = ""
    disc_number: Optional[int] = None
    include_artist_in_filename: bool = False

    def render_folder(
        self,
        *,
        localize_category: Callable[[str], str] = lambda value: value,
        disc_label: Callable[[int], str] = lambda number: f"Disc {number}",
    ) -> str:
        parts = [self.artist_folder]
        if self.category_folder:
            parts.append(localize_category(self.category_folder))
        parts.append(self.collection_folder)
        if self.disc_number:
            parts.append(disc_label(self.disc_number))
        return "/".join(_dedupe(parts))


def decide_output_layout(
    *,
    source_kind: object,
    release_type: object,
    collection_title: object,
    album: object,
    parent_artist: object,
    artist: object,
    category: object = "",
    total_tracks: object = 0,
    platform: object = "",
    track_title: object = "",
    playlist_subfolders: bool = True,
    singles_subfolder: bool = True,
    multi_disc: bool = False,
    disc_number: object = 0,
) -> OutputLayoutDecision:
    """Choose a deterministic folder and filename body for one occurrence."""

    kind = _kind_name(source_kind)
    release = _text(release_type).casefold()
    collection = _text(collection_title)
    album_name = _text(album)
    root_artist = _text(parent_artist) or _text(artist)
    raw_category = _text(category)
    platform_name = _text(getattr(platform, "value", platform)).casefold()
    title = _text(track_title).casefold()

    try:
        count = max(0, int(total_tracks or 0))
    except (TypeError, ValueError):
        count = 0
    try:
        disc = int(disc_number or 0)
    except (TypeError, ValueError):
        disc = 0
    disc = disc if multi_disc and disc > 0 else None

    is_direct = kind in {"SINGLE_VIDEO", "UNKNOWN"}
    include_artist = bool(root_artist) and (
        is_direct or release in {"compilation", "appears_on"}
    )

    if not playlist_subfolders or is_direct:
        return OutputLayoutDecision(
            include_artist_in_filename=include_artist,
        )

    # Direct collections must use their collection identity.  Per-track album
    # metadata is only a last-resort compatibility fallback.
    if kind in {"PLAYLIST", "ALBUM"}:
        folder = collection or album_name or (
            "Playlist" if kind == "PLAYLIST" else "Album"
        )
        return OutputLayoutDecision(
            collection_folder=folder,
            disc_number=disc if kind == "ALBUM" else None,
            include_artist_in_filename=include_artist,
        )

    # Artist/channel imports use a stable Artist / Category / Collection tree.
    if kind == "ARTIST":
        if release == "album":
            category_name = "אלבומים"
        elif release in {"compilation", "appears_on"}:
            category_name = raw_category or "אוספים"
        elif release == "playlist":
            category_name = "פלייליסטים"
        elif release == "video":
            category_name = raw_category or "סרטונים"
        elif release == "performance" or (
            ("live" in title or "הופעה" in title) and platform_name != "spotify"
        ):
            category_name = "הופעות חיות"
        elif raw_category.casefold() in _SINGLES_CATEGORIES:
            category_name = "סינגלים ומיני אלבומים"
        elif raw_category:
            category_name = raw_category
        else:
            category_name = "סינגלים ומיני אלבומים"

        grouped = release in {"album", "ep", "compilation", "appears_on", "playlist"}
        grouped = grouped or bool(count > 1 and (collection or album_name))
        release_folder = (collection or album_name) if grouped else ""

        if category_name == "סינגלים ומיני אלבומים" and not singles_subfolder:
            return OutputLayoutDecision(
                artist_folder=root_artist,
                collection_folder=release_folder,
                disc_number=disc if grouped else None,
                include_artist_in_filename=include_artist,
            )

        return OutputLayoutDecision(
            artist_folder=root_artist,
            category_folder=category_name,
            collection_folder=release_folder,
            disc_number=disc if grouped else None,
            include_artist_in_filename=include_artist,
        )

    # Legacy cards without source context retain a conservative collection
    # folder only when it was explicitly persisted. Descriptive album tags on
    # old direct-song cards must not invent a collection after an upgrade.
    return OutputLayoutDecision(
        collection_folder=collection,
        disc_number=disc,
        include_artist_in_filename=include_artist,
    )
