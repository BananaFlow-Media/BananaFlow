"""Shared artist-catalog discovery and duplicate-resolution primitives.

The GUI uses this module for Spotify and YouTube Music artist imports.  It
keeps provider/network work behind small lazy entry points while the catalog
models and duplicate policy remain plain Python and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import unicodedata
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import parse_qs, urlparse

from core.playlist_parser import SourcePlatform, UrlKind


_SECTION_ICONS = {
    "album": "💿",
    "single": "🎵",
    "performance": "🎤",
    "video": "🎬",
    "playlist": "📋",
    "compilation": "🗂",
    "appears_on": "✨",
    "all": "🎧",
}


@dataclass
class ArtistCatalogSection:
    """One provider-owned artist category available for import."""

    key: str
    provider_label: str = ""
    item_count: int = -1
    releases: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def icon(self) -> str:
        return _SECTION_ICONS.get(self.key, "🎧")


@dataclass
class ArtistCatalogDiscovery:
    """Result of the cheap category-discovery phase."""

    url: str
    platform: SourcePlatform
    artist_name: str = ""
    sections: list[ArtistCatalogSection] = field(default_factory=list)
    error: str = ""

    def section(self, key: str) -> Optional[ArtistCatalogSection]:
        return next((section for section in self.sections if section.key == key), None)


@dataclass
class CatalogDuplicateGroup:
    """Occurrences that represent the same or probably the same recording."""

    group_id: str
    title: str
    confidence: str  # "exact" | "probable"
    indices: tuple[int, ...]


def discover_artist_catalog(
    url: str,
    platform: SourcePlatform,
    *,
    locale: str = "en-US",
) -> ArtistCatalogDiscovery:
    """Discover the categories that actually exist for one artist URL."""

    result = ArtistCatalogDiscovery(url=url, platform=platform)
    try:
        if platform == SourcePlatform.YOUTUBE_MUSIC:
            from utils.ytm_scraper import discover_ytm_artist_catalog

            artist_name, raw_sections = discover_ytm_artist_catalog(url)
            result.artist_name = artist_name
            result.sections = [
                ArtistCatalogSection(
                    key=key,
                    provider_label=key,
                    item_count=len(releases),
                    releases=tuple(releases),
                )
                for key, releases in raw_sections.items()
                if releases
            ]
        elif platform == SourcePlatform.SPOTIFY:
            from core.scraper import discover_spotify_artist_sections

            artist_name, raw_sections = discover_spotify_artist_sections(
                url, locale=locale,
            )
            result.artist_name = artist_name
            result.sections = [
                ArtistCatalogSection(
                    key=section["key"],
                    provider_label=section.get("provider_label", ""),
                    item_count=int(section.get("item_count", -1)),
                )
                for section in raw_sections
            ]
        else:
            result.error = "Unsupported artist catalog platform."
    except Exception as exc:  # noqa: BLE001 - returned to the UI as discovery failure
        result.error = str(exc) or exc.__class__.__name__

    if not result.error and not result.sections:
        result.error = "No importable artist categories were found."
    return result


def scrape_artist_catalog(
    discovery: ArtistCatalogDiscovery,
    selected_keys: Iterable[str],
    *,
    locale: str = "en-US",
    cookies_file: Optional[str] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    on_section: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """Expand only selected categories and return queue-ready track dicts."""

    selected = set(selected_keys)
    if not selected:
        return []

    if discovery.platform == SourcePlatform.YOUTUBE_MUSIC:
        from core.scraper import scrape_ytm_artist

        releases: list[dict] = []
        for section in discovery.sections:
            if section.key not in selected:
                continue
            if on_section:
                on_section(section.key)
            releases.extend(dict(release) for release in section.releases)
        _title, tracks = scrape_ytm_artist(
            discovery.url,
            releases=releases,
            cancel_check=cancel_check,
        )
    elif discovery.platform == SourcePlatform.SPOTIFY:
        from core.scraper import scrape_spotify_artist

        labels = {
            section.key: section.provider_label
            for section in discovery.sections
            if section.key in selected
        }
        _title, tracks = scrape_spotify_artist(
            discovery.url,
            cookies_file=cookies_file,
            metadata_only=True,
            cancel_check=cancel_check,
            locale=locale,
            selected_sections=labels,
            on_section=on_section,
        )
    else:
        return []

    platform_name = discovery.platform.value
    for track in tracks:
        track.setdefault("platform", platform_name)
        track.setdefault("source_kind", UrlKind.ARTIST.name)
        track.setdefault("source_url", discovery.url)
        if not track.get("duration_str") and track.get("duration_sec"):
            seconds = int(track["duration_sec"])
            minutes, seconds = divmod(seconds, 60)
            hours, minutes = divmod(minutes, 60)
            track["duration_str"] = (
                f"{hours}:{minutes:02d}:{seconds:02d}"
                if hours else f"{minutes}:{seconds:02d}"
            )
        if discovery.platform == SourcePlatform.SPOTIFY:
            if not track.get("url"):
                query = f"{track.get('artist', '')} {track.get('title', '')}".strip()
                track["url"] = f"ytsearch1:{query} audio"
            track.setdefault("match_status", "pending")
    return deduplicate_catalog_occurrences(tracks)


def _identity_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _youtube_video_id(track: Mapping[str, object]) -> str:
    direct = str(track.get("source_id") or track.get("video_id") or "").strip()
    if direct:
        return direct
    raw_url = str(track.get("url") or track.get("track_url") or "")
    parsed = urlparse(raw_url)
    if parsed.hostname and parsed.hostname.casefold() in {
        "youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be",
    }:
        if parsed.hostname.casefold() == "youtu.be":
            return parsed.path.strip("/")
        return (parse_qs(parsed.query).get("v") or [""])[0]
    return ""


def _stable_identity(track: Mapping[str, object]) -> str:
    platform = str(track.get("platform") or "").casefold()
    spotify_id = str(track.get("spotify_id") or "").strip()
    if platform == "spotify" and spotify_id:
        return f"spotify:{spotify_id}"
    video_id = _youtube_video_id(track)
    if video_id:
        return f"youtube:{video_id}"
    return ""


def _catalog_location(track: Mapping[str, object]) -> tuple[str, str, int]:
    """Return the release occurrence that owns one catalog row.

    Category alone is not a location: the same recording can occur in two
    different albums, singles or compilations inside the same category.  A
    provider release id is preferred; older/fallback scrapers use the release
    title.  Track position keeps a deliberate repeated recording inside one
    release reviewable instead of silently collapsing it.
    """
    section = _identity_text(
        track.get("catalog_section") or track.get("category") or track.get("release_type")
    )
    release = _identity_text(
        track.get("source_release_id")
        or track.get("release_id")
        or track.get("album")
        or track.get("release_title")
    )
    try:
        position = int(track.get("album_index") or track.get("track_number") or 0)
    except (TypeError, ValueError):
        position = 0
    return section, release, position


def _probable_base(track: Mapping[str, object]) -> tuple[str, str]:
    return (
        _identity_text(track.get("title")),
        _identity_text(track.get("artist")),
    )


def _duration(track: Mapping[str, object]) -> int:
    try:
        return int(float(track.get("duration_sec") or 0))
    except (TypeError, ValueError):
        return 0


def deduplicate_catalog_occurrences(tracks: Iterable[dict]) -> list[dict]:
    """Collapse an exact row emitted twice for the same release position.

    This is deliberately narrower than recording duplicate detection.  It
    protects artist imports when a provider repeats a shelf/release while a
    paginated or virtualised view is being expanded, but preserves the same
    recording in a different release so the user can choose its metadata and
    output location in the conflict dialog.
    """
    result: list[dict] = []
    seen: set[tuple[str, tuple[str, str, int]]] = set()
    for track in tracks:
        identity = _stable_identity(track)
        if not identity:
            base = _probable_base(track)
            duration = _duration(track)
            if not all(base):
                result.append(track)
                continue
            identity = f"metadata:{base[0]}:{base[1]}:{duration}"
        occurrence = (identity, _catalog_location(track))
        if occurrence in seen:
            continue
        seen.add(occurrence)
        result.append(track)
    return result


def detect_catalog_duplicates(tracks: list[dict]) -> list[CatalogDuplicateGroup]:
    """Group exact IDs and conservative title/artist/duration matches.

    Probable matches are review-only.  Version markers remain part of the
    normalized title, so ``Live``, ``Remix`` and ``Remaster`` variants are not
    collapsed into an unqualified studio title.
    """

    if len(tracks) < 2:
        return []

    parent = list(range(len(tracks)))
    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        l_root, r_root = find(left), find(right)
        if l_root != r_root:
            parent[r_root] = l_root

    by_stable: dict[str, list[int]] = {}
    for index, track in enumerate(tracks):
        identity = _stable_identity(track)
        if identity:
            by_stable.setdefault(identity, []).append(index)
    for indices in by_stable.values():
        for index in indices[1:]:
            union(indices[0], index)

    by_metadata: dict[tuple[str, str], list[int]] = {}
    for index, track in enumerate(tracks):
        base = _probable_base(track)
        if all(base) and _duration(track) > 0:
            by_metadata.setdefault(base, []).append(index)
    for indices in by_metadata.values():
        for offset, left in enumerate(indices):
            for right in indices[offset + 1:]:
                if abs(_duration(tracks[left]) - _duration(tracks[right])) <= 2:
                    union(left, right)

    components: dict[int, list[int]] = {}
    for index in range(len(tracks)):
        components.setdefault(find(index), []).append(index)

    groups: list[CatalogDuplicateGroup] = []
    for indices in components.values():
        if len(indices) < 2:
            continue
        locations = {_catalog_location(tracks[index]) for index in indices}
        if len(locations) < 2:
            continue
        stable_values = {_stable_identity(tracks[index]) for index in indices}
        exact = len(stable_values) == 1 and "" not in stable_values
        groups.append(CatalogDuplicateGroup(
            group_id=f"catalog-duplicate-{indices[0]}",
            title=str(tracks[indices[0]].get("title") or "Unknown"),
            confidence="exact" if exact else "probable",
            indices=tuple(indices),
        ))
    groups.sort(key=lambda group: group.title.casefold())
    return groups


def apply_catalog_decisions(
    tracks: list[dict],
    groups: Iterable[CatalogDuplicateGroup],
    decisions: Mapping[str, Iterable[int]],
) -> list[dict]:
    """Keep non-conflicts plus the occurrences selected for each conflict."""

    keep = set(range(len(tracks)))
    for group in groups:
        chosen = set(decisions.get(group.group_id, group.indices))
        keep.difference_update(group.indices)
        keep.update(index for index in chosen if index in group.indices)
    return [track for index, track in enumerate(tracks) if index in keep]
