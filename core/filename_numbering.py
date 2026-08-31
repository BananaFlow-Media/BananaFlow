"""Provider-neutral filename and track-position numbering policy.

Queue order is presentation state, not collection metadata. This module turns
the original provider position plus source/release context into two independent
decisions:

* ``filename_index`` controls the optional ``NN - `` filename prefix.
* ``metadata_track_index`` controls the embedded track-number tag.

Keeping those decisions separate lets compilations retain authoritative track
metadata without forcing a numbered physical filename.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class NumberingDecision:
    """The numbering values to carry into one download request."""

    filename_index: Optional[int] = None
    metadata_track_index: Optional[int] = None
    is_release_position: bool = False


def _positive_int(value: object) -> Optional[int]:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _kind_name(value: object) -> str:
    name = getattr(value, "name", value)
    return str(name or "").strip().upper()


def decide_numbering(
    *,
    source_kind: object,
    release_type: object,
    collection_index: object,
    total_tracks: object = 0,
    number_playlists: bool = True,
) -> NumberingDecision:
    """Return filename and tag numbering for one source occurrence.

    ``collection_index`` is the provider's original position in the album,
    EP, compilation or playlist. It must never be a GUI queue ordinal.
    """

    kind = _kind_name(source_kind)
    release = str(release_type or "").strip().casefold()
    index = _positive_int(collection_index)
    total = _positive_int(total_tracks) or 0

    # A direct track remains independent even if descriptive metadata happens
    # to name an album or expose a track position.
    if kind in {"SINGLE_VIDEO", "UNKNOWN"}:
        return NumberingDecision()

    # YouTube/YouTube Music extractors do not always label a direct collection
    # even though URL classification already knows what it is.
    if not release:
        if kind == "ALBUM":
            release = "album"
        elif kind == "PLAYLIST":
            release = "playlist"

    # YouTube Music exposes the Singles & EPs shelf as ``single``. Once the
    # release expands to multiple tracks it is an EP for numbering purposes.
    if release == "single" and total > 1:
        release = "ep"
    elif release == "appears_on":
        release = "compilation"

    if release in {"album", "ep"}:
        return NumberingDecision(
            filename_index=index,
            metadata_track_index=index,
            is_release_position=index is not None,
        )

    if release == "compilation":
        return NumberingDecision(
            filename_index=None,
            metadata_track_index=index,
            is_release_position=index is not None,
        )

    if release == "playlist":
        return NumberingDecision(
            filename_index=index if number_playlists else None,
            metadata_track_index=None,
            is_release_position=False,
        )

    # Standalone singles, performances and videos inside an artist import are
    # independent files rather than ordered releases.
    return NumberingDecision()
