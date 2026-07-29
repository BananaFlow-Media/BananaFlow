"""Typed failures for recording matching and stale-target recovery."""

from __future__ import annotations

import re


class SpotifyMetadataInvalid(ValueError):
    """Spotify did not provide a trustworthy track title and artist credit set."""


_MEDIA_UNAVAILABLE_RE = re.compile(
    r"private video|video unavailable|has been removed|no longer available|"
    r"video (?:was )?deleted|this video is unavailable",
    re.I,
)


def is_media_unavailable_error(message: str) -> bool:
    """True only when a different upload may solve the failure.

    Authentication, bot challenges, rate limits, geo restrictions and format
    errors intentionally do not match: changing recording candidates would
    hide the real recovery action and could select different content.
    """
    text = message or ""
    # A private/deleted upload is stale even when yt-dlp appends generic
    # sign-in advice. Check those exact states before the broader exclusions.
    if re.search(r"private video|has been removed|video (?:was )?deleted", text, re.I):
        return True
    if re.search(
        r"(?:not available|unavailable) in (?:your )?(?:country|region)|geo.?restrict|"
        r"sign in to confirm|not a bot|bot challenge|"
        r"\b429\b|too many requests|rate.?limit|throttl|\b403\b|forbidden|"
        r"account required|login required|age.?restrict",
        text,
        re.I,
    ):
        return False
    return bool(_MEDIA_UNAVAILABLE_RE.search(text))
