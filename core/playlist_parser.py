"""
playlist_parser.py  –  Playlist & single-URL metadata extractor
================================================================
Responsibilities
----------------
* Detect whether a URL is a single item or a playlist/album.
* Extract structured metadata for every item WITHOUT downloading any media.
* Emit incremental item-ready callbacks so the GUI can populate a list
  progressively (no waiting for 500-track playlists to finish resolving).
* Handle YouTube playlists, YouTube Music playlists, and Spotify
  (tracks, albums, playlists) through yt-dlp's built-in extractors.

Design contract
---------------
* Zero GUI imports – identical rule as downloader.py.
* All I/O is done through callbacks and return values (no global state).
* `ParseResult` and `TrackMeta` are plain dataclasses – safe to pickle,
  copy, or send across a queue to the UI thread.
* The parser can be cancelled mid-flight via `PlaylistParser.cancel()`.

Typical usage
-------------
>>> parser = PlaylistParser()
>>> result = parser.parse(
...     url="https://www.youtube.com/playlist?list=PLxxxxxx",
...     on_item=lambda item, idx, total: print(f"[{idx}/{total}] {item.title}"),
...     on_progress=lambda msg: print(msg),
... )
>>> print(result.summary())
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse, parse_qs

import yt_dlp
try:
    import yt_dlp_ejs  # noqa: F401  – loads QuickJS runtime for YouTube PO-token
except ImportError:
    pass

from utils.time_format import seconds_to_str as _seconds_to_str
from utils.logger import SilentLogger as _SilentLogger
from utils.yt_dlp_opts import (
    build_parse_ydl_opts as _build_parse_ydl_opts,
    private_cookie_ydl_opts,
)
from utils.artwork_cleaner import clean_artwork_url
from utils.url_cleaner import host_matches_domain

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Public data-classes
# ──────────────────────────────────────────────────────────────────────────────

class SourcePlatform(Enum):
    YOUTUBE         = "youtube"
    YOUTUBE_MUSIC   = "ytmusic"
    SPOTIFY         = "spotify"
    GENERIC         = "generic"
    UNKNOWN         = "unknown"


class UrlKind(Enum):
    SINGLE_VIDEO    = auto()   # One video / track
    PLAYLIST        = auto()   # YouTube / YT-Music playlist
    ALBUM           = auto()   # Spotify album
    ARTIST          = auto()   # Spotify artist discography (rare)
    UNKNOWN         = auto()


@dataclass
class TrackMeta:
    """
    Normalised metadata for one video/track entry.
    All fields default to safe empty values so the UI can always render
    something even when the extractor returns incomplete data.
    """
    # ── Identity ─────────────────────────────────────────────────────────────
    index:          int   = 0           # 1-based position in the result list (for sorting)
    album_index:    int   = 0           # 1-based position in the album/release (for filename)
    disc_number:    int   = 0           # 1-based disc, 0 = no disc context (multi-disc releases)
    url:            str   = ""          # canonical watch/track URL
    title:          str   = "Unknown Title"
    artist:         str   = ""          # uploader / artist name
    album:          str   = ""          # album / playlist title
    parent_artist:  str   = ""          # cleaned root artist for organization
    release_type:   str   = ""          # "album", "single", "playlist", or "performance"

    # ── Timing ───────────────────────────────────────────────────────────────
    duration_sec:   Optional[int]   = None   # None = live / unknown
    duration_str:   str             = ""     # human-readable "3:45"

    # ── Visuals ──────────────────────────────────────────────────────────────
    thumbnail_url:  str   = ""          # best-quality thumbnail

    # ── Platform ─────────────────────────────────────────────────────────────
    platform:       SourcePlatform = SourcePlatform.UNKNOWN
    source_kind:    str   = ""          # UrlKind.name of the URL that produced this item
    source_url:     str   = ""          # original fetched URL / collection identity
    
    # ── Custom Categories ───────────────────────────────────────────────────
    category:       str   = ""          # override for custom yt/scraper tabs
    total_tracks:   int   = 0           # total tracks in the release (for EP grouping)

    # ── Deferred-match state (Spotify two-stage import) ───────────────────────
    # In the two-stage flow the catalog is shown immediately with metadata
    # only; the YouTube match is resolved later, at download time. Until then
    # ``url`` holds a ``ytsearch*`` placeholder and ``match_status`` is
    # "pending". Non-Spotify paths resolve up-front and stay "matched".
    spotify_id:       str = ""            # stable Spotify track id (cache key)
    spotify_key_kind: str = "spotify_id"  # "spotify_id" | "composite"
    match_status:     str = "matched"     # matched | pending | metadata_invalid | unresolved
    resolution_error: str = ""            # stable UI translation key for blocked rows

    # ── State used by the GUI (not set by the parser) ─────────────────────────
    selected:       bool = True         # pre-tick all items in the UI

    # ── Helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def format_duration(seconds: Optional[int]) -> str:
        """Convert raw seconds to MM:SS or HH:MM:SS string."""
        return _seconds_to_str(seconds, live_label="Live")

    def __post_init__(self) -> None:
        # Auto-fill duration_str when duration_sec is provided
        if self.duration_sec is not None and not self.duration_str:
            self.duration_str = self.format_duration(self.duration_sec)


@dataclass
class ParseResult:
    """
    Container returned by `PlaylistParser.parse()` after extraction finishes.
    """
    url:            str
    kind:           UrlKind
    platform:       SourcePlatform
    playlist_title: str             = ""
    playlist_id:    str             = ""
    total_count:    int             = 0     # reported by extractor (may differ from len(tracks))
    tracks:         list[TrackMeta] = field(default_factory=list)
    error:          str             = ""    # non-empty → partial or full failure
    cancelled:      bool            = False

    def success(self) -> bool:
        return not self.error and not self.cancelled and bool(self.tracks)

    def summary(self) -> str:
        if self.cancelled:
            return f"Cancelled after {len(self.tracks)} item(s)."
        if self.error:
            return f"Parse error: {self.error}"
        total_sec = sum(t.duration_sec or 0 for t in self.tracks)
        h, rem    = divmod(total_sec, 3600)
        m, s      = divmod(rem, 60)
        duration  = f"{h}h {m}m" if h else f"{m}m {s}s"
        return (
            f'"{self.playlist_title}" – {len(self.tracks)} tracks, '
            f"~{duration} total"
        )


# ──────────────────────────────────────────────────────────────────────────────
# URL classifier  (pure regex, no network)
# ──────────────────────────────────────────────────────────────────────────────

_YT_PLAYLIST_RE  = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
_YT_VIDEO_RE     = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")
_YTM_RE          = re.compile(r"music\.youtube\.com")
_SPOTIFY_RE      = re.compile(
    r"open\.spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)"
)
# Common pattern for user/channel video listing pages across many sites
_USER_LISTING_RE = re.compile(r"/users?/[^/]+/(videos?|uploads?|playlists?|content)", re.IGNORECASE)

# Hostname/domain shape, with or without a scheme: "youtube.com", "example.co.uk".
# Requires an ASCII TLD-like ending, so e.g. a Hebrew netloc ("https://ברקוני")
# is rejected even though urlparse() happily accepts it as a syntactic netloc.
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,62}\.)+[a-zA-Z]{2,63}$")
_IPV4_RE     = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# A generic "scheme://" prefix (http, https, or anything else): a strong
# signal the user *intended* to paste a URL, even if what follows is broken.
_SCHEME_ATTEMPT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def _valid_host(host: str) -> bool:
    """True if ``host`` (a netloc or bare domain, no path) looks like a real hostname."""
    host = host.strip().split(":", 1)[0]  # drop a trailing :port
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    return bool(_HOSTNAME_RE.match(host)) or bool(_IPV4_RE.match(host))


def looks_like_url(text: str) -> bool:
    """
    Heuristic check for "is this navigable as a URL" vs. free-text search input.
    The URL bar doubles as a search box (its placeholder invites either), so the
    UI needs this to decide whether to fetch the text directly or forward it to
    the Search tab instead of handing raw non-URL text to Playwright/yt-dlp.
    """
    text = text.strip()
    if not text or " " in text:
        return False
    parsed = urlparse(text)
    if parsed.scheme in ("http", "https"):
        return bool(parsed.netloc) and _valid_host(parsed.netloc)
    if not parsed.scheme:
        return _valid_host(text.split("/", 1)[0])
    return False


def is_malformed_url_attempt(text: str) -> bool:
    """
    True when the text clearly *tries* to be a URL (starts with a
    "scheme://" prefix like "https://") but fails looks_like_url — e.g. a
    truncated paste or a typo'd address. The UI should tell the user their
    URL is invalid in this case rather than silently forwarding it to
    search, which is reserved for plain text that was never meant as a URL.
    """
    text = text.strip()
    return bool(_SCHEME_ATTEMPT_RE.match(text)) and not looks_like_url(text)


def classify_url(url: str) -> tuple[SourcePlatform, UrlKind]:
    """
    Classify a URL into (platform, kind) without making any network calls.
    Used by the UI to show the right icon / label before parsing starts.
    """
    if _YTM_RE.search(url):
        platform = SourcePlatform.YOUTUBE_MUSIC
        # YTM Browse IDs usually start with UC (Artist/Channel) or FDbrowse (Custom sections)
        # Album/Playlist Browse IDs usually start with MPSP, MPRE, or occur in a playlist list= context.
        is_browse = "/browse/" in url or "/channel/" in url or "/@" in url
        has_list = _YT_PLAYLIST_RE.search(url)
        list_id = has_list.group(1) if has_list else ""
        
        if is_browse:
            # Check for common Album ID prefixes in the URL path segment after /browse/
            path_segments = urlparse(url).path.split("/")
            browse_id = path_segments[-1] if path_segments else ""
            if browse_id.startswith(("MPRE", "MPSP", "OLAK")):
                kind = UrlKind.ALBUM
            elif browse_id.startswith(("UC", "FDbrowse", "channel", "@")):
                kind = UrlKind.ARTIST
            else:
                # Ambiguous browse URL, default to PLAYLIST/ALBUM for fallback standard parsing
                kind = UrlKind.ALBUM if has_list else UrlKind.ARTIST
        else:
            if list_id.startswith("OLAK"):
                kind = UrlKind.ALBUM
            elif has_list:
                kind = UrlKind.PLAYLIST
            else:
                kind = UrlKind.SINGLE_VIDEO
        return platform, kind

    if host_matches_domain(urlparse(url).netloc, "youtube.com", "youtu.be"):
        platform = SourcePlatform.YOUTUBE
        if _YT_PLAYLIST_RE.search(url):
            # Could be a video-in-playlist (v=...&list=...) or pure playlist
            kind = UrlKind.PLAYLIST
        elif _YT_VIDEO_RE.search(url):
            kind = UrlKind.SINGLE_VIDEO
        elif re.search(r"youtube\.com/(@[^/?]+|channel/[^/?]+|user/[^/?]+|c/[^/?]+)", url):
            kind = UrlKind.ARTIST
        else:
            kind = UrlKind.UNKNOWN
        return platform, kind

    m = _SPOTIFY_RE.search(url)
    if m:
        type_map = {
            "track":    UrlKind.SINGLE_VIDEO,
            "album":    UrlKind.ALBUM,
            "playlist": UrlKind.PLAYLIST,
            "artist":   UrlKind.ARTIST,
        }
        return SourcePlatform.SPOTIFY, type_map.get(m.group(1), UrlKind.UNKNOWN)

    # User/channel video listing pages on any site → treat as playlist
    if _USER_LISTING_RE.search(url):
        return SourcePlatform.GENERIC, UrlKind.PLAYLIST

    # Any valid http/https URL → let yt-dlp's Generic extractor try
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return SourcePlatform.GENERIC, UrlKind.UNKNOWN

    return SourcePlatform.UNKNOWN, UrlKind.UNKNOWN


# ──────────────────────────────────────────────────────────────────────────────
# Thumbnail selector  (best quality, safe fallback)
# ──────────────────────────────────────────────────────────────────────────────

def _best_thumbnail(info: dict) -> str:
    """
    Pick the highest-resolution thumbnail URL from a yt-dlp info dict.
    Falls back gracefully through multiple possible keys.
    """
    # yt-dlp may provide a ranked list of thumbnails
    thumbnails: list[dict] = info.get("thumbnails") or []
    if thumbnails:
        # Sort by resolution (width * height) descending; prefer HTTPS
        def _score(t: dict) -> int:
            w = t.get("width")  or 0
            h = t.get("height") or 0
            return w * h

        ranked = sorted(
            [t for t in thumbnails if t.get("url")],
            key=_score,
            reverse=True,
        )
        if ranked:
            return ranked[0]["url"]

    # Direct thumbnail key as last resort
    return info.get("thumbnail") or ""


# ──────────────────────────────────────────────────────────────────────────────
# Info-dict → TrackMeta  normaliser
# ──────────────────────────────────────────────────────────────────────────────

def _entry_to_track(
    entry:    dict,
    index:    int,
    platform: SourcePlatform,
    album:    str = "",
) -> TrackMeta:
    """
    Convert one yt-dlp entry dict into a normalised TrackMeta.
    Works for both fully-resolved entries and lightweight flat-playlist entries
    (where only title / id / url are guaranteed to be present).
    """
    # URL: prefer webpage_url, fall back to reconstructed YouTube URL (YouTube only)
    url = (
        entry.get("webpage_url")
        or entry.get("url")
        or (
            f"https://www.youtube.com/watch?v={entry['id']}"
            if entry.get("id") and platform in (
                SourcePlatform.YOUTUBE, SourcePlatform.YOUTUBE_MUSIC
            )
            else ""
        )
    )
    from utils.url_cleaner import clean_youtube_url
    url = clean_youtube_url(url)

    duration_sec: Optional[int] = entry.get("duration")
    if duration_sec is not None:
        try:
            duration_sec = int(duration_sec)
        except (TypeError, ValueError):
            duration_sec = None

    artist = (
        entry.get("artist")
        or entry.get("creator")
        or entry.get("uploader")
        or ""
    )
    
    from utils.metadata_cleaner import clean_title_and_artist
    raw_title = entry.get("title") or entry.get("fulltitle") or "Unknown Title"
    cleaned_title, cleaned_artist = clean_title_and_artist(raw_title, artist)

    return TrackMeta(
        index=index,
        url=url,
        title=cleaned_title,
        artist=cleaned_artist,
        album=(entry.get("album") or entry.get("playlist_title") or album or "").strip(),
        duration_sec=duration_sec,
        duration_str=TrackMeta.format_duration(duration_sec),
        thumbnail_url=clean_artwork_url(_best_thumbnail(entry), platform),
        platform=platform,
        release_type=entry.get("release_type", ""),
        album_index=entry.get("album_index", 0) or entry.get("playlist_index", 0),
        disc_number=entry.get("disc_number", 0),
        selected=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main parser class
# ──────────────────────────────────────────────────────────────────────────────

class PlaylistParser:
    """
    Metadata-only extractor.  Never downloads media.

    Callbacks
    ---------
    on_item(track, index, total)
        Called as soon as each TrackMeta is resolved.
        `total` is the count reported by the extractor; it may be None for
        sources that don't announce total count upfront (e.g. Spotify).

    on_progress(message)
        Called with human-readable status strings, e.g. "Fetching item 3/50".
        Useful for a status-bar label in the GUI.

    on_error(message)
        Called for non-fatal per-item errors (private videos, geo-blocks…).
        The parser continues after calling this; fatal errors are returned
        as ParseResult.error instead.

    Threading
    ---------
    `parse()` is blocking.  Use `parse_async()` for non-blocking use.
    Call `cancel()` from any thread to abort mid-flight.
    """

    def __init__(self) -> None:
        self._cancel = threading.Event()

    # ── Public API ─────────────────────────────────────────────────────────────

    def parse(
        self,
        url:             str,
        *,
        cookies_file:    Optional[str]                               = None,
        proxy_url:       Optional[str]                               = None,
        proxy_token:     Optional[str]                               = None,
        channel_tabs:    Optional[list[str]]                         = None,
        on_item:         Optional[Callable[[TrackMeta, int, Optional[int]], None]] = None,
        on_progress:     Optional[Callable[[str], None]]             = None,
        on_error:        Optional[Callable[[str], None]]             = None,
    ) -> ParseResult:
        """
        Blocking metadata extraction.  Returns a fully-populated ParseResult.
        """
        self._cancel.clear()
        platform, kind = classify_url(url)
        logger.debug(f"PlaylistParser.parse: url={url}, platform={platform.name}, kind={kind.name}")
        result = ParseResult(url=url, kind=kind, platform=platform)

        self._notify(on_progress, f"Analysing URL… ({platform.name})")

        # ── DISPATCH TO MODULAR SCRAPERS ──────────────────────────────────────
        from core.scraper import (
            scrape_spotify_playlist, scrape_spotify_album, scrape_spotify_artist,
            scrape_ytm_playlist, scrape_ytm_album, scrape_ytm_artist,
            scrape_youtube_playlist, scrape_youtube_channel,
        )

        idx_counter = [0]
        def _on_scraper_item(track_data: dict) -> None:
            if self._cancel.is_set(): return
            idx_counter[0] += 1
            idx = idx_counter[0]
            item_url = track_data.get("url", "")
            # The scraper/resolver is the authority on whether a track has been
            # matched to YouTube yet. In the two-stage flow the Spotify scrapers
            # emit metadata-only items tagged ``match_status="pending"`` (URL is a
            # ``ytsearch*`` placeholder, resolved lazily at download time);
            # everything else defaults to "matched".
            match_status = track_data.get("match_status", "matched")
            track = TrackMeta(
                index=idx,
                url=item_url,
                title=track_data.get("title", f"Item {idx}"),
                artist=track_data.get("artist", ""),
                album=track_data.get("album", ""),
                parent_artist=track_data.get("parent_artist") or "",
                release_type=track_data.get("release_type", ""),
                category=track_data.get("category", ""),
                album_index=track_data.get("album_index", 0),
                disc_number=track_data.get("disc_number", 0),
                thumbnail_url=track_data.get("thumbnail_url", ""),
                duration_str=track_data.get("duration_str", ""),
                duration_sec=track_data.get("duration_sec"),
                total_tracks=track_data.get("total_tracks", 0),
                spotify_id=track_data.get("spotify_id", ""),
                spotify_key_kind=track_data.get("spotify_key_kind", "spotify_id"),
                match_status=match_status,
                resolution_error=track_data.get("resolution_error", ""),
                platform=platform,
                source_kind=kind.name,
                source_url=url,
                selected=match_status not in ("metadata_invalid", "unresolved"),
            )
            result.tracks.append(track)
            if on_item:
                try: on_item(track, idx, None)
                except Exception: pass

        try:
            # ── SPOTIFY ──────────────────────────────────────────────────────
            if platform == SourcePlatform.SPOTIFY:
                from ui.i18n import current_language
                from core.scraper import (
                    scrape_spotify_playlist, scrape_spotify_album,
                    scrape_spotify_artist, scrape_spotify_track
                )
                spotify_locale = "he-IL" if current_language() == "he" else "en-US"
                if kind == UrlKind.SINGLE_VIDEO:
                    title, _ = scrape_spotify_track(
                        url, on_item=_on_scraper_item, cookies_file=cookies_file,
                        locale=spotify_locale,
                    )
                elif kind in (UrlKind.PLAYLIST, UrlKind.ALBUM, UrlKind.ARTIST):
                    # Two-stage flow: show the whole catalog immediately with
                    # metadata only; the YouTube match is deferred to download
                    # time (see DownloadRequest.url_resolver). Try the metadata
                    # resolver first, fall back to the metadata-only Playwright
                    # scrape when it is not configured / fails.
                    title = self._scrape_spotify_catalog(
                        url, kind, on_item=_on_scraper_item,
                        on_progress=on_progress, cookies_file=cookies_file,
                        locale=spotify_locale,
                    )
                    if kind == UrlKind.ARTIST:
                        title = f"{title} (Discography)"
                else:
                    title, _ = scrape_spotify_track(
                        url, on_item=_on_scraper_item, cookies_file=cookies_file,
                        locale=spotify_locale,
                    )
                result.playlist_title = title

            # ── YOUTUBE MUSIC ────────────────────────────────────────────────
            elif platform == SourcePlatform.YOUTUBE_MUSIC:
                from core.scraper import (
                    scrape_ytm_playlist, scrape_ytm_album, 
                    scrape_ytm_artist, scrape_ytm_track
                )
                if kind == UrlKind.SINGLE_VIDEO:
                    title, _ = scrape_ytm_track(url, on_item=_on_scraper_item)
                elif kind == UrlKind.PLAYLIST:
                    title, _ = scrape_ytm_playlist(url, on_item=_on_scraper_item)
                elif kind == UrlKind.ALBUM:
                    title, _ = scrape_ytm_album(url, on_item=_on_scraper_item)
                elif kind == UrlKind.ARTIST:
                    title, _ = scrape_ytm_artist(url, on_item=_on_scraper_item)
                    title = f"{title} (Discography)"
                else:
                    title, _ = scrape_ytm_track(url, on_item=_on_scraper_item)
                result.playlist_title = title

            # ── YOUTUBE ──────────────────────────────────────────────────────
            elif platform == SourcePlatform.YOUTUBE:
                # yt-dlp handles all YouTube URL shapes (videos, playlists, channels,
                # @handles) natively and reliably — use it for everything.
                result = self._parse_standard_yt(
                    url,
                    platform=platform,
                    kind=kind,
                    cookies_file=cookies_file,
                    on_item=on_item,
                    on_progress=on_progress,
                    on_error=on_error,
                )
            
            # ── GENERIC / OTHER ──────────────────────────────────────────────
            else:
                result = self._parse_standard_yt(
                    url,
                    platform=platform,
                    kind=kind,
                    cookies_file=cookies_file,
                    on_item=on_item,
                    on_progress=on_progress,
                    on_error=on_error,
                )
                # If yt-dlp found nothing, try the paginated listing scraper
                # (handles user/channel listing pages on any site)
                if not result.tracks and not result.cancelled:
                    self._notify(on_progress, "Trying paginated listing scraper…")
                    try:
                        from core.listing_scraper import scrape_listing_page
                        title, _ = scrape_listing_page(
                            url,
                            on_item=_on_scraper_item,
                            cancel_check=lambda: self._cancel.is_set(),
                        )
                        if result.tracks:
                            result.playlist_title = title
                            result.kind = UrlKind.PLAYLIST
                    except Exception:
                        pass
                # Final fallback: Playwright stream interception (single video pages)
                if not result.tracks and not result.cancelled:
                    result = self._parse_universal_fallback(
                        url,
                        existing_result=result,
                        on_item=on_item,
                        on_progress=on_progress,
                        on_error=on_error,
                    )

            if result.total_count == 0:
                result.total_count = len(result.tracks)
            if result.total_count == 1 and result.tracks:
                result.playlist_title = result.tracks[0].title
            
            self._notify(on_progress, f"Successfully resolved {result.total_count} items.")

        except Exception as exc:
            result.error = str(exc)
            if on_error:
                try: on_error(str(exc))

                except Exception: pass
        return result

    def parse_async(
        self,
        url:          str,
        *,
        cookies_file: Optional[str]                               = None,
        on_item:      Optional[Callable[[TrackMeta, int, Optional[int]], None]] = None,
        on_progress:  Optional[Callable[[str], None]]            = None,
        on_error:     Optional[Callable[[str], None]]            = None,
        on_done:      Optional[Callable[[ParseResult], None]]    = None,
        daemon:       bool                                        = True,
    ) -> threading.Thread:
        """
        Non-blocking wrapper.  `on_done` is called with the final ParseResult
        once the thread finishes (or is cancelled).
        """
        def _run() -> None:
            result = self.parse(
                url,
                cookies_file=cookies_file,
                on_item=on_item,
                on_progress=on_progress,
                on_error=on_error,
            )
            if on_done:
                try:
                    on_done(result)
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=_run, daemon=daemon, name=f"parse-{id(url)}")
        t.start()
        return t

    def cancel(self) -> None:
        """Signal the running parse to stop after the current item."""
        self._cancel.set()

    # ── Spotify two-stage catalog ──────────────────────────────────────────────

    def _scrape_spotify_catalog(
        self,
        url:         str,
        kind:        "UrlKind",
        on_item:     Callable[[dict], None],
        on_progress: Optional[Callable[[str], None]],
        cookies_file: Optional[str],
        locale:       str = "en-US",
    ) -> str:
        """Stage 1 of the two-stage Spotify import.

        Publishes every track of an artist / album / playlist **progressively**
        as it is scraped, with metadata only — no YouTube matching yet (that is
        deferred to download time via ``DownloadRequest.url_resolver``). Uses the
        metadata-only Playwright scrape, which fills the UI incrementally and is
        promptly cancellable via ``cancel_check`` (a configured proxy would be a
        single blocking call — neither progressive nor cancellable mid-flight —
        so it is intentionally not used for this stage). Returns the catalog
        title.
        """
        from ui.i18n import t
        from core.scraper import (
            scrape_spotify_playlist, scrape_spotify_album, scrape_spotify_artist,
        )
        cancel_cb = self._cancel.is_set
        count = [0]

        def _pending_on_item(track_data: dict) -> None:
            # Every stage-1 item is unmatched by definition.
            track_data.setdefault("match_status", "pending")
            count[0] += 1
            on_item(track_data)

        self._notify(on_progress, t("collecting_catalog"))

        scraper = {
            UrlKind.PLAYLIST: scrape_spotify_playlist,
            UrlKind.ALBUM:    scrape_spotify_album,
            UrlKind.ARTIST:   scrape_spotify_artist,
        }.get(kind, scrape_spotify_playlist)
        title, _ = scraper(
            url, on_item=_pending_on_item, cookies_file=cookies_file,
            metadata_only=True, cancel_check=cancel_cb, locale=locale,
        )
        self._notify(on_progress, t("found_n_tracks", n=count[0]))
        return title

    # ── yt-dlp options ─────────────────────────────────────────────────────────

    @staticmethod
    def _build_opts(cookies_file: Optional[str], logger: Optional[_SilentLogger] = None) -> dict:
        """Build yt-dlp options using the shared builder (DRY)."""
        return _build_parse_ydl_opts(
            cookies_file=cookies_file,
            logger=logger or _SilentLogger(),
        )

    def _parse_standard_yt(
        self,
        url: str,
        *,
        platform: SourcePlatform,
        kind: UrlKind,
        cookies_file: Optional[str],
        on_item: Optional[Callable[[TrackMeta, int, Optional[int]], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> ParseResult:
        """Parse via yt-dlp directly with robust error propagation."""
        result = ParseResult(url=url, kind=kind, platform=platform)
        extract_logger = _SilentLogger()
        ydl_opts = self._build_opts(cookies_file=cookies_file, logger=extract_logger)

        try:
            with private_cookie_ydl_opts(ydl_opts) as private_opts:
                with yt_dlp.YoutubeDL(private_opts) as ydl:
                    info = ydl.extract_info(url, download=False)

            if not info:
                # build_parse_ydl_opts sets ignoreerrors=True, so a real extractor
                # failure (private/removed/restricted video, etc.) doesn't raise —
                # it lands in extract_logger instead and info comes back empty.
                # Surface that real reason instead of a meaningless placeholder,
                # so classify_error() downstream can match it to a specific,
                # actionable message instead of the generic fallback.
                real_reason = (
                    (extract_logger.errors or extract_logger.warnings or [None])[-1]
                )
                result.error = real_reason or "Failed to extract metadata (empty response from yt-dlp)."
                return result

            entries = info.get("entries") if isinstance(info, dict) else None
            if entries:
                playlist_title = info.get("title") or info.get("playlist_title") or "Unknown Playlist"
                result.playlist_title = playlist_title
                for idx, entry in enumerate((e for e in entries if e), start=1):
                    if self._cancel.is_set():
                        result.cancelled = True
                        break
                    track = _entry_to_track(entry, idx, platform=platform, album=playlist_title)
                    track.source_kind = kind.name
                    track.source_url = url
                    result.tracks.append(track)
                    if on_item:
                        on_item(track, idx, len(entries))
            else:
                track = _entry_to_track(info, 1, platform=platform, album="")
                track.source_kind = kind.name
                track.source_url = url
                result.tracks.append(track)
                result.playlist_title = track.title
                if on_item:
                    on_item(track, 1, 1)

            result.total_count = len(result.tracks)
            return result
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or exc.__class__.__name__
            result.error = message
            self._notify(on_progress, f"Metadata extraction failed: {message}")
            if on_error:
                try:
                    on_error(message)
                except Exception:  # noqa: BLE001
                    pass
            return result

    # ── Universal (Playwright) fallback ───────────────────────────────────────

    def _parse_universal_fallback(
        self,
        url: str,
        *,
        existing_result: ParseResult,
        on_item:     Optional[Callable[[TrackMeta, int, Optional[int]], None]] = None,
        on_progress: Optional[Callable[[str], None]] = None,
        on_error:    Optional[Callable[[str], None]] = None,
    ) -> ParseResult:
        """
        Second-pass fallback when yt-dlp found nothing.

        Strategy:
        1. Try the generic listing scraper — opens the page and finds all video
           page links (works for any site: adult, news, etc.).
           Each link becomes a TrackMeta with category='stream_intercept',
           meaning the downloader will open THAT page and intercept its HLS stream.

        2. If no video links found (single video page or obscure layout):
           Fall back to Playwright stream interception of the current page directly
           (old behaviour — intercepts the HLS that the page itself loads).
        """
        result = existing_result
        self._notify(on_progress, "yt-dlp found nothing — scanning page for video links…")

        # ── Step 1: Generic listing scraper ───────────────────────────────────
        try:
            from core.universal_extractor import scrape_generic_video_listing
            links = scrape_generic_video_listing(url, timeout_ms=30_000)
        except ImportError:
            links = []
        except Exception as exc:
            logger.debug("[PlaylistParser] generic_listing failed: %s", exc)
            links = []

        if links:
            self._notify(
                on_progress,
                f"Found {len(links)} video link(s) — will intercept streams at download time"
            )
            # Derive playlist title from page or URL
            from urllib.parse import urlparse as _up
            parsed = _up(url)
            playlist_title = (
                parsed.path.rstrip("/").split("/")[-1].replace("-", " ")
                or parsed.netloc
                or url
            )
            result.playlist_title = playlist_title

            for idx, link in enumerate(links, start=1):
                if self._cancel.is_set():
                    result.cancelled = True
                    break
                track = TrackMeta(
                    index=idx,
                    url=link["url"],
                    title=link["title"] or f"Video {idx}",
                    artist="",
                    thumbnail_url=link.get("thumbnail_url", ""),
                    platform=SourcePlatform.GENERIC,
                    # 'stream_intercept' tells the downloader to open this video
                    # page and intercept its HLS/DASH stream at download time.
                    category="stream_intercept",
                    source_kind=kind.name,
                    source_url=url,
                )
                result.tracks.append(track)
                if on_item:
                    try:
                        on_item(track, idx, len(links))
                    except Exception:
                        pass

            result.total_count = len(result.tracks)
            return result

        # ── Step 2: Direct stream interception (single video page) ────────────
        self._notify(on_progress, "No video links found — intercepting streams directly…")

        try:
            from core.universal_extractor import find_streams
        except ImportError:
            self._notify(on_error, "Playwright not installed — universal extraction unavailable")
            return result

        try:
            streams = find_streams(url, timeout_ms=25_000)
        except Exception as exc:
            self._notify(on_error, f"Playwright interception failed: {exc}")
            return result

        if not streams:
            result.error = "No media streams found (yt-dlp and Playwright both returned nothing)"
            return result

        self._notify(
            on_progress,
            f"Found {len(streams)} media stream(s) via page interception"
        )

        # Derive a title from the page title or the last path segment of the URL
        page_title = streams[0].page_title if streams else ""
        if not page_title:
            from urllib.parse import urlparse as _up2
            parsed2 = _up2(url)
            page_title = parsed2.path.split("/")[-1] or parsed2.netloc or url

        result.playlist_title = page_title

        for idx, stream in enumerate(streams, start=1):
            if self._cancel.is_set():
                result.cancelled = True
                break
            track = TrackMeta(
                index=idx,
                url=stream.url,
                title=page_title if len(streams) == 1 else f"{page_title} [{idx}]",
                artist="",
                platform=SourcePlatform.GENERIC,
                # Carry the stream type in the category field so DownloadRequest
                # can route to the correct downloader without a schema change.
                category=f"stream:{stream.stream_type}",
                source_kind=kind.name,
                source_url=url,
            )
            result.tracks.append(track)
            if on_item:
                try:
                    on_item(track, idx, len(streams))
                except Exception:
                    pass

        result.total_count = len(result.tracks)
        return result

    # ── Utility ────────────────────────────────────────────────────────────────

    @staticmethod
    def _notify(callback: Optional[Callable], message: str) -> None:
        if callback:
            try:
                callback(message)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[PlaylistParser] _notify callback raised: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Convenience one-shot function  (for callers that don't need an instance)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_playlist_metadata(
    url:          str,
    *,
    cookies_file: Optional[str]  = None,
    on_item:      Optional[Callable[[TrackMeta, int, Optional[int]], None]] = None,
    on_progress:  Optional[Callable[[str], None]]  = None,
    on_error:     Optional[Callable[[str], None]]  = None,
) -> ParseResult:
    """
    Module-level convenience wrapper.  Creates a parser, runs it, returns
    the result.  No way to cancel – use PlaylistParser directly for that.

    Returns
    -------
    ParseResult
        .tracks  → list[TrackMeta]  each item has:
                   .index           int          1-based position
                   .title           str
                   .artist          str
                   .album           str          playlist / album name
                   .duration_sec    int | None   raw seconds
                   .duration_str    str          "3:45" or "1:02:30"
                   .url             str          watch / track URL
                   .thumbnail_url   str          best thumbnail URL
                   .platform        SourcePlatform
                   .selected        bool         True (UI default)
    """
    return PlaylistParser().parse(
        url,
        cookies_file=cookies_file,
        on_item=on_item,
        on_progress=on_progress,
        on_error=on_error,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI smoke-test  (python playlist_parser.py <url>)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    _url = (
        sys.argv[1] if len(sys.argv) > 1
        else "https://www.youtube.com/playlist?list=EXAMPLE_PLAYLIST_ID"
    )

    _platform, _kind = classify_url(_url)
    print(f"URL classified as → platform={_platform.name}  kind={_kind.name}\n")

    _col_w = (4, 42, 22, 8, 10)   # column widths

    def _header() -> None:
        print(
            f"{'#':<{_col_w[0]}}  "
            f"{'Title':<{_col_w[1]}}  "
            f"{'Artist':<{_col_w[2]}}  "
            f"{'Duration':<{_col_w[3]}}  "
            f"{'Thumbnail?':<{_col_w[4]}}"
        )
        print("─" * sum(_col_w + (2,) * len(_col_w)))

    _header_printed = False

    def _on_item(track: TrackMeta, idx: int, total: Optional[int]) -> None:
        global _header_printed
        if not _header_printed:
            _header()
            _header_printed = True
        has_thumb = "✓" if track.thumbnail_url else "✗"
        total_str = str(total) if total else "?"
        print(
            f"{idx:<{_col_w[0]}}  "
            f"{track.title[:_col_w[1]]:<{_col_w[1]}}  "
            f"{track.artist[:_col_w[2]]:<{_col_w[2]}}  "
            f"{track.duration_str:<{_col_w[3]}}  "
            f"{has_thumb:<{_col_w[4]}}"
        )

    def _on_progress(msg: str) -> None:
        print(f"  ℹ  {msg}")

    def _on_error(msg: str) -> None:
        print(f"  ⚠  {msg}", file=sys.stderr)

    _result = fetch_playlist_metadata(
        _url,
        on_item=_on_item,
        on_progress=_on_progress,
        on_error=_on_error,
    )

    print()
    print("=" * 60)
    print(_result.summary())

    if _result.error:
        print(f"Error detail: {_result.error}", file=sys.stderr)
        sys.exit(1)
