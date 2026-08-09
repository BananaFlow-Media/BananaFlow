"""
core/spotify_request_builder.py — the Spotify two-stage matching contract,
in one framework-agnostic place.

A Spotify album / playlist / artist import is a TWO-STAGE flow: stage 1
publishes the whole catalog with metadata only (``match_status="pending"``
and a ``ytsearch*`` placeholder URL — see ``core.scraper._emit_pending_track``),
and stage 2 matches each track to YouTube lazily, in the instant before its
download starts, through ``DownloadRequest.url_resolver``.

Wiring that second stage onto a request used to live exclusively inside
``ui.controllers.download_controller``. ``cli.py`` therefore built its
requests straight from ``track.url`` and shipped the ``ytsearch1:`` placeholder
to yt-dlp — which downloads *something* (the first free-text search hit) while
bypassing the scorer, the album-aware search chain and the persistent match
cache the desktop app uses. The two front-ends could pick different files for
the same album, and CLI-based performance work measured a path the product
does not actually use (issue #59).

Everything a caller needs to reproduce the desktop behaviour is here:

* :func:`spotify_identity`     — the minimal identity dict a match needs.
* :func:`build_spotify_resolver` — the lazy resolver closure itself.
* :func:`attach_spotify_matching` — the one place that decides whether a
  request gets a resolver, an identity, or neither.
* :func:`effective_match_status` / :func:`is_downloadable` — the shared
  admission rule for a track that cannot be matched at all.

The accessors read through ``getattr`` *or* ``__getitem__`` so the same code
serves all three shapes the app carries a track in: a live ``TrackCard``
widget (GUI), a ``TrackMeta`` dataclass (parser/CLI) and the plain dict a
paused job persists (``DownloadController._card_to_dict``).

This module must stay importable without Qt: the CLI depends on it.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from core.playlist_parser import SourcePlatform

# A track in one of these states has no usable target and never will without
# fresh metadata: "metadata_invalid" failed validation at scrape time (it
# carries an empty URL), and "unresolved" is a match that already failed.
BLOCKED_MATCH_STATUSES = ("metadata_invalid", "unresolved")


def _get(source: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from an object attribute or a mapping key."""
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _platform_name(source: Any) -> str:
    """The track's platform as a lowercase string.

    ``TrackCard.platform`` is already a string; ``TrackMeta.platform`` is a
    ``SourcePlatform``. Both normalise to the enum's value ("spotify", …).
    """
    platform = _get(source, "platform", "")
    if isinstance(platform, SourcePlatform):
        return platform.value
    return str(platform or "").lower()


def spotify_identity(source: Any) -> dict:
    """The minimal, serialisable identity one Spotify track is matched by.

    These exact keys are what ``core.scraper.resolve_track_to_youtube``
    consumes: ``spotify_id``/``spotify_key_kind`` select the match-cache row,
    and title/album/artist/duration_sec drive the search chain and the scorer.
    Keep it in sync with ``DownloadController._card_to_dict``, which persists
    the same fields so a paused job can rebuild an equivalent resolver.
    """
    return {
        "spotify_id":       _get(source, "spotify_id", "") or "",
        "spotify_key_kind": _get(source, "spotify_key_kind", "spotify_id") or "spotify_id",
        "title":            _get(source, "title", "") or "",
        "album":            _get(source, "album", "") or "",
        "artist":           _get(source, "artist", "") or "",
        "duration_sec":     _get(source, "duration_sec", None),
    }


def build_spotify_resolver(
    identity: dict,
    cookies_file: Optional[str] = None,
) -> Callable[[Optional[threading.Event]], str]:
    """Build the lazy ``url_resolver`` closure for one pending Spotify track.

    The returned callable takes the request's cancel Event (so a cancel stops
    an in-flight match) and returns a resolved, cleaned YouTube/YTM URL.

    It carries a ``resolve_source`` attribute — "cache" / "prefetched" /
    "live" / "shared" — which the orchestrator reads to decide how much
    resolver concurrency a batch needs. It is seeded from a local-only cache
    peek (no network) and re-stamped with the truth once the match has run.

    The closure cannot be persisted; a paused job re-creates an equivalent one
    from the identity fields it saved. That is exactly why the identity dict,
    not the closure, is the thing this module treats as the contract.
    """
    from core.scraper import track_match_source_hint

    source_hint = track_match_source_hint(identity)

    def _resolve(ev, _td=identity, _cookies=cookies_file):
        from core.scraper import resolve_track_to_youtube
        from utils.url_cleaner import clean_youtube_url as _clean
        resolved = resolve_track_to_youtube(
            _td, cookies_file=_cookies,
            cancel_check=lambda: ev is not None and ev.is_set(),
        )
        _resolve.resolve_source = _td.get("_match_source", "live")
        return _clean(resolved)

    _resolve.resolve_source = source_hint
    return _resolve


def attach_spotify_matching(
    req,                       # DownloadRequest
    source: Any,               # TrackCard | TrackMeta | persisted card dict
    cookies_file: Optional[str] = None,
) -> bool:
    """Attach the Spotify matching contract to a freshly built request.

    Returns True when a lazy resolver was attached (a stage-2 match will run
    at download time), False otherwise.

    Three cases, and every front-end must agree on all three:

    * ``match_status == "pending"`` → the two-stage case. The request gets
      both a resolver and the identity behind it.
    * an already-matched Spotify track (single-track imports resolve up
      front) → identity only. No match is needed now, but if that exact
      upload later proves private/deleted the orchestrator uses the identity
      to invalidate just that cache row and match once more.
    * anything else (YouTube, YT-Music, generic) → untouched.
    """
    # effective_match_status, not the raw field: a Spotify track being retried
    # after a failed match still reads "unresolved" on a caller that does not
    # write the reset back (the CLI), and it needs a resolver just the same.
    if effective_match_status(source) == "pending":
        identity = spotify_identity(source)
        # The resolver owns `identity` and stamps "_match_source" into it as a
        # side effect; the request keeps a copy so the recorded identity stays
        # exactly the six contract fields.
        req.url_resolver = build_spotify_resolver(identity, cookies_file)
        req.spotify_match_identity = dict(identity)
        return True

    if _get(req, "platform", None) == SourcePlatform.SPOTIFY:
        req.spotify_match_identity = spotify_identity(source)

    return False


def effective_match_status(source: Any) -> str:
    """The match_status to act on, after the Spotify retry rule.

    A Spotify track left "unresolved" by a previous failed match is not
    permanently broken — the network, the cookies or the catalog may all have
    changed since. Asking to download it again means asking for another match
    attempt, so it reads back as "pending". Callers holding mutable state (the
    GUI's cards) should write the reset back; stateless callers can just use
    the returned value.
    """
    status = str(_get(source, "match_status", "matched") or "matched")
    if status == "unresolved" and _platform_name(source) == "spotify":
        return "pending"
    return status


def is_downloadable(source: Any, status: Optional[str] = None) -> bool:
    """Whether a track can start a download at all.

    A "pending" track passes with no usable URL — resolving one is the whole
    point of stage 2. Everything else needs a real URL: a blocked status, or
    a matched track whose URL is missing, has nothing to hand to yt-dlp.

    ``status`` may be supplied by a caller that already computed (and acted
    on) :func:`effective_match_status`, so the retry rule is applied once.
    """
    status = status or effective_match_status(source)
    if status in BLOCKED_MATCH_STATUSES:
        return False
    if status == "pending":
        return True
    # TrackCard spells it "track_url"; TrackMeta and the persisted dict "url".
    url = _get(source, "track_url", "") or _get(source, "url", "") or ""
    return bool(str(url).strip())
