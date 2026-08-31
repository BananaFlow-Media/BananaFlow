"""Provider-neutral filename-numbering policy contracts."""

from __future__ import annotations

import pytest

from core.filename_numbering import NumberingDecision, decide_numbering
from core.playlist_parser import UrlKind


@pytest.mark.parametrize(
    ("source_kind", "release_type", "index", "total", "enabled", "expected"),
    [
        (UrlKind.SINGLE_VIDEO, "single", 7, 1, True, NumberingDecision()),
        (UrlKind.UNKNOWN, "album", 7, 10, True, NumberingDecision()),
        (UrlKind.ALBUM, "album", 3, 12, True, NumberingDecision(3, 3, True)),
        (UrlKind.ALBUM, "album", 3, 12, False, NumberingDecision(3, 3, True)),
        # Missing YTM release labels fall back to the classified URL kind.
        (UrlKind.ALBUM, "", 4, 12, False, NumberingDecision(4, 4, True)),
        (UrlKind.ARTIST, "ep", 2, 4, False, NumberingDecision(2, 2, True)),
        # A multi-track YTM Singles & EPs release is an EP for numbering.
        (UrlKind.ARTIST, "single", 2, 4, True, NumberingDecision(2, 2, True)),
        (UrlKind.ARTIST, "single", 1, 1, True, NumberingDecision()),
        (UrlKind.ARTIST, "performance", 1, 1, True, NumberingDecision()),
        (UrlKind.ARTIST, "video", 1, 1, True, NumberingDecision()),
        # Compilations keep authoritative tag metadata but no filename prefix.
        (UrlKind.ARTIST, "compilation", 5, 20, True, NumberingDecision(None, 5, True)),
        (UrlKind.ARTIST, "appears_on", 5, 20, True, NumberingDecision(None, 5, True)),
        (UrlKind.PLAYLIST, "playlist", 8, 30, True, NumberingDecision(8, None, False)),
        (UrlKind.PLAYLIST, "playlist", 8, 30, False, NumberingDecision()),
        (UrlKind.PLAYLIST, "", 8, 30, True, NumberingDecision(8, None, False)),
        (UrlKind.ARTIST, "playlist", 6, 15, True, NumberingDecision(6, None, False)),
        (UrlKind.ARTIST, "playlist", 6, 15, False, NumberingDecision()),
    ],
)
def test_numbering_policy(
    source_kind, release_type, index, total, enabled, expected,
):
    assert decide_numbering(
        source_kind=source_kind,
        release_type=release_type,
        collection_index=index,
        total_tracks=total,
        number_playlists=enabled,
    ) == expected


@pytest.mark.parametrize("bad_index", [None, 0, -1, "", "invalid"])
def test_invalid_collection_position_never_falls_back_to_queue_order(bad_index):
    assert decide_numbering(
        source_kind=UrlKind.PLAYLIST,
        release_type="playlist",
        collection_index=bad_index,
        number_playlists=True,
    ) == NumberingDecision()
