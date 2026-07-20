"""
core/duplicate_detector.py  –  Cross-tab duplicate detection for channel downloads
===================================================================================
Pure functions — no Qt, no I/O, no threading.  Fast enough to run on the main
thread after scraping completes.

A "duplicate" is a video whose YouTube ID appears in more than one scraped source
(e.g. in "סרטונים" AND inside a playlist from "פלייליסטים").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class VideoInfo:
    """Scraped metadata for one video from one source location."""
    video_id:       str
    title:          str
    url:            str
    thumbnail_url:  str
    duration_sec:   Optional[int]
    tab_name:       str   # e.g. "סרטונים", "קצרים", "פלייליסטים"
    tab_type:       str   # "videos" | "shorts" | "streams" | "releases" | "playlist_item"

    # Playlist-specific fields (only set when tab_type == "playlist_item")
    playlist_name:  str = ""
    playlist_url:   str = ""
    playlist_index: int = 0   # 1-based position within the playlist


@dataclass
class DuplicateGroup:
    """
    One video that appears in more than one scraped location.

    ``appearances`` is the full list of VideoInfo objects — one per occurrence.
    If the same video is in "סרטונים" AND in two different playlists, there
    will be three entries.
    """
    video_id:    str
    title:       str
    appearances: list[VideoInfo] = field(default_factory=list)

    def tab_names(self) -> list[str]:
        """Unique tab names (preserving first-seen order)."""
        seen: list[str] = []
        for a in self.appearances:
            key = f"{a.tab_name}:{a.playlist_name}" if a.playlist_name else a.tab_name
            if key not in seen:
                seen.append(key)
        return seen

    def is_cross_tab(self) -> bool:
        """True when the duplication crosses different tab types (not just two playlists)."""
        top_tabs = {a.tab_name for a in self.appearances}
        return len(top_tabs) > 1


# ── Decision model ─────────────────────────────────────────────────────────────

@dataclass
class DuplicateDecision:
    """
    User's resolution for one DuplicateGroup.

    ``keep_keys`` is a set of "tab_name:playlist_name" strings (or just
    "tab_name" for non-playlist sources) that the user wants to KEEP.
    Any appearance whose key is NOT in keep_keys will be excluded from
    the final download list.
    """
    video_id:  str
    keep_keys: set[str] = field(default_factory=set)

    @classmethod
    def keep_all(cls, group: DuplicateGroup) -> "DuplicateDecision":
        return cls(video_id=group.video_id, keep_keys=set(_appearance_key(a) for a in group.appearances))

    @classmethod
    def keep_non_playlist(cls, group: DuplicateGroup) -> "DuplicateDecision":
        """Keep only the non-playlist occurrence(s)."""
        keys = {_appearance_key(a) for a in group.appearances if a.tab_type != "playlist_item"}
        if not keys:
            keys = {_appearance_key(group.appearances[0])}  # safety fallback
        return cls(video_id=group.video_id, keep_keys=keys)

    @classmethod
    def keep_playlist(cls, group: DuplicateGroup) -> "DuplicateDecision":
        """Keep only the playlist occurrence(s)."""
        keys = {_appearance_key(a) for a in group.appearances if a.tab_type == "playlist_item"}
        if not keys:
            keys = {_appearance_key(group.appearances[-1])}  # safety fallback
        return cls(video_id=group.video_id, keep_keys=keys)


def _appearance_key(a: VideoInfo) -> str:
    return f"{a.tab_name}:{a.playlist_name}" if a.playlist_name else a.tab_name


# ── Detection ──────────────────────────────────────────────────────────────────

def detect_duplicates(tab_results: dict[str, list[VideoInfo]]) -> list[DuplicateGroup]:
    """
    Find all video IDs that appear in more than one scraped source.

    Parameters
    ----------
    tab_results : {tab_name: [VideoInfo, ...]}
                  The complete scraping output from ChannelScrapeWorker.
                  Playlist items live under the "פלייליסטים" key but carry
                  their individual playlist names in VideoInfo.playlist_name.

    Returns
    -------
    List of DuplicateGroup objects, sorted by title.
    Empty list means no duplicates → no conflict dialog needed.
    """
    # Accumulate all appearances keyed by video_id
    by_id: dict[str, list[VideoInfo]] = {}
    for videos in tab_results.values():
        for v in videos:
            if not v.video_id:
                continue
            by_id.setdefault(v.video_id, []).append(v)

    groups = [
        DuplicateGroup(video_id=vid, title=vids[0].title, appearances=vids)
        for vid, vids in by_id.items()
        if len(vids) > 1
    ]
    groups.sort(key=lambda g: g.title.lower())
    return groups


# ── Filtering ──────────────────────────────────────────────────────────────────

def apply_decisions(
    tab_results: dict[str, list[VideoInfo]],
    decisions:   list[DuplicateDecision],
) -> dict[str, list[VideoInfo]]:
    """
    Filter tab_results according to the user's conflict decisions.

    Videos that are NOT in any DuplicateGroup pass through unchanged.
    For videos that ARE duplicates, only the appearances whose key is in
    decision.keep_keys survive.

    Returns a new dict with the same structure as tab_results.
    """
    # Build lookup: {video_id: DuplicateDecision}
    decision_map = {d.video_id: d for d in decisions}

    filtered: dict[str, list[VideoInfo]] = {}
    for tab_name, videos in tab_results.items():
        kept: list[VideoInfo] = []
        for v in videos:
            dec = decision_map.get(v.video_id)
            if dec is None:
                kept.append(v)  # not a duplicate — keep unconditionally
            elif _appearance_key(v) in dec.keep_keys:
                kept.append(v)  # user chose to keep this appearance
            # else: user dropped this appearance — skip it
        filtered[tab_name] = kept

    return filtered
