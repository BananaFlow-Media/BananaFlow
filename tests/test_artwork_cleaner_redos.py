"""tests/test_artwork_cleaner_redos.py — regression test for a real ReDoS
found during Phase 11's automated release-candidate audit (CodeQL alert
py/redos, utils/artwork_cleaner.py).

The original pattern's trailing group, ``(-[a-z0-9-]+)*$``, let a run of
dashes be split across repetitions in exponentially many ways once the
overall match failed to anchor at ``$`` — confirmed hanging >30s on a
~30-byte crafted input. Fixed by excluding ``-`` from the repeated group's
character class (``(-[a-z0-9]+)*$``), removing the ambiguity.
"""
from __future__ import annotations

import time

import pytest

from core.playlist_parser import SourcePlatform
from utils.artwork_cleaner import clean_artwork_url


class TestReDoSFixed:
    @pytest.mark.parametrize("n", [30, 200, 2000])
    def test_pathological_suffix_does_not_hang(self, n):
        """A crafted non-matching tail must resolve near-instantly, not
        exhibit catastrophic backtracking."""
        evil_url = "https://lh3.googleusercontent.com/abc=w1" + "-a" * n + "!"
        t0 = time.monotonic()
        clean_artwork_url(evil_url, SourcePlatform.YOUTUBE_MUSIC)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"clean_artwork_url took {elapsed:.2f}s — regex is not ReDoS-safe"


class TestBehaviorPreserved:
    """The fix must not change output for real, legitimate thumbnail URLs."""

    def test_full_size_suffix_upgraded(self):
        url = "https://lh3.googleusercontent.com/abc=w120-h120-l90-rj"
        result = clean_artwork_url(url, SourcePlatform.YOUTUBE_MUSIC)
        assert result == "https://lh3.googleusercontent.com/abc=w1200-h1200-p-rj"

    def test_short_size_suffix_upgraded(self):
        url = "https://lh3.googleusercontent.com/abc=s120-c"
        result = clean_artwork_url(url, SourcePlatform.YOUTUBE_MUSIC)
        assert result == "https://lh3.googleusercontent.com/abc=w1200-h1200-p-rj"

    def test_dash_prefixed_suffix_upgraded(self):
        url = "https://yt3.ggpht.com/abc-w120-h120"
        result = clean_artwork_url(url, SourcePlatform.YOUTUBE_MUSIC)
        assert result == "https://yt3.ggpht.com/abc-w1200-h1200-p-rj"

    def test_no_suffix_appends_default(self):
        url = "https://lh3.googleusercontent.com/abc"
        result = clean_artwork_url(url, SourcePlatform.YOUTUBE_MUSIC)
        assert result == "https://lh3.googleusercontent.com/abc=w1200-h1200-p-rj"

    def test_empty_url_returns_empty(self):
        assert clean_artwork_url("", SourcePlatform.YOUTUBE_MUSIC) == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
