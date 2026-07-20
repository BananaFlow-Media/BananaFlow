"""
utils/spotify_resolver.py  –  Spotify metadata resolver
=========================================================
Resolves Spotify URLs (track / album / playlist / artist) into a list of
plain-Python dicts that the rest of the app can use to build DownloadRequests
pointing at YouTube search strings.

Two resolution strategies are supported:

1. **Web API (preferred)** – Uses the official Spotify Web API with the
   client_credentials OAuth 2.0 flow.  Requires a free Spotify Developer
   account and a registered application.  Credentials are read from
   ``AppConfig.spotify_client_id`` / ``AppConfig.spotify_client_secret``.
   This gives access to full pagination, artist discographies, thumbnails,
   and accurate metadata.

2. **Embed API fallback** – The original approach: fetches the public embed
   page (``open.spotify.com/embed/{type}/{id}``) and parses the
   ``__NEXT_DATA__`` JSON payload.  Requires no credentials but is fragile
   (Spotify can change the embed payload structure at any time) and does not
   support artist discographies.

Design
------
* Zero GUI imports – pure stdlib + urllib/requests.  Safe from any thread.
* Token caching: the access token is stored as a class variable so multiple
  calls within the same process share a single token and only refresh when
  it expires.
* Rate limiting: ``_api_get()`` retries once with a 1-second back-off on
  HTTP 429 responses.
* Pagination: album/playlist resolvers loop until the API returns
  ``"next": null``.
* Progressive callbacks: ``resolve_artist()`` accepts an ``on_item`` callable
  that is invoked for each resolved track so the UI can populate
  incrementally without waiting for entire discographies to finish.

Output format
-------------
Every resolver method returns ``list[dict]`` with these keys:
    title        str  – track title
    artist       str  – primary artist name
    url          str  – ``ytsearch1:<artist> <title> audio`` search string
    duration_sec int | None  – track duration in seconds
    thumbnail_url str  – album art URL (best available; empty string if none)
    spotify_url  str  – original Spotify track URL for reference
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import logging
from typing import Callable, Optional


# ── Spotify API constants ──────────────────────────────────────────────────────

_TOKEN_URL    = "https://accounts.spotify.com/api/token"
_API_BASE     = "https://api.spotify.com/v1"
_EMBED_BASE   = "https://open.spotify.com/embed"
_REQUEST_UA   = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


# ── Global Logger ─────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# SpotifyResolver
# ──────────────────────────────────────────────────────────────────────────────

class SpotifyResolver:
    """
    Resolves Spotify URLs into YouTube-search-string dicts.

    All public methods are class/static methods so callers don't need to
    instantiate the class.  Token state is stored at the class level so it
    is shared across all call sites in the same process.

    Usage
    -----
    >>> from utils.spotify_resolver import SpotifyResolver
    >>> tracks = SpotifyResolver.resolve("https://open.spotify.com/album/xyz")
    >>> for t in tracks:
    ...     print(t["artist"], "–", t["title"])
    """

    # ── Class-level token cache ───────────────────────────────────────────────
    _token:        str   = ""
    _token_expiry: float = 0.0     # epoch seconds
    _token_lock:   threading.Lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def resolve(
        cls,
        url:         str,
        on_item:     Optional[Callable[[dict], None]] = None,
        proxy_url:   Optional[str] = None,
        proxy_token: Optional[str] = None,
    ) -> list[dict]:
        """
        Resolve any Spotify URL to a list of track dicts.

        Parameters
        ----------
        url     : Spotify URL (track / album / playlist / artist).
        on_item : Optional callback invoked for each resolved track.
                  Useful for progressive population of the UI.  The dict
                  passed is the same format as a list element in the return
                  value.

        Returns
        -------
        list[dict]
            Each dict has keys: title, artist, url, duration_sec,
            thumbnail_url, spotify_url.

        Raises
        ------
        ValueError  – url does not match a recognised Spotify pattern.
        RuntimeError – network / parse failure.
        """
        logger.debug(f"SpotifyResolver.resolve: url={url}")
        # Clean URL: strip query params and trailing slashes for clean matching
        clean_url = url.split("?")[0].rstrip("/")
        match = re.search(
            r"open\.spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)",
            clean_url,
        )
        if not match:
            # Try a more permissive regex if the above fails
            match = re.search(r"/(track|album|playlist|artist)/([A-Za-z0-9_-]+)", url)

        if not match:
            logger.debug(f"SpotifyResolver: URL Match FAILED for {url}")
            raise ValueError(f"Invalid or unsupported Spotify URL: {url!r}")

        entity_type = match.group(1)
        entity_id   = match.group(2)

        # Use passed config or fallback to global AppConfig
        if not proxy_url or not proxy_token:
            cfg_url, cfg_token = cls._get_proxy_config()
            proxy_url   = proxy_url or cfg_url
            proxy_token = proxy_token or cfg_token

        if proxy_url and proxy_url.lower().startswith(("http://", "https://")):
            try:
                return cls._resolve_proxy(url, proxy_url, proxy_token, on_item)
            except RuntimeError as err:
                if "Rate Limit" in str(err) or "429" in str(err):
                    if entity_type == "artist":
                        logger.warning("[SpotifyResolver] Spotify Rate Limited. Falling back to local YTM resolution for artist...")
                        return cls._resolve_artist_ytm_fallback(url, entity_id, on_item)
                raise

        raise RuntimeError(
            "Spotify Proxy is not configured.\n\n"
            "Please go to Settings → Spotify and set your Proxy URL and App API Key."
        )

    @classmethod
    def resolve_artist(
        cls,
        url:     str,
        on_item: Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Convenience alias: resolve an artist URL to their full discography.
        Raises RuntimeError if credentials are not configured.
        """
        return cls.resolve(url, on_item=on_item)

    # ──────────────────────────────────────────────────────────────────────────
    # Web API resolvers
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _resolve_track_api(
        cls,
        track_id: str,
        on_item:  Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """Fetch a single track from the Spotify Web API."""
        data  = cls._api_get(f"/tracks/{track_id}")
        items = [cls._track_dict_from_api(data)]
        if on_item and items:
            on_item(items[0])
        return items

    @classmethod
    def _resolve_album_api(
        cls,
        album_id: str,
        on_item:  Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Fetch all tracks in an album, paginated in groups of 50.
        Also fetches the album artwork and primary artist name.
        """
        # Get album-level metadata first (title, artwork, artist)
        album_data = cls._api_get(f"/albums/{album_id}")
        album_name = album_data.get("name", "")
        album_art  = cls._best_image(album_data.get("images", []))
        artist     = cls._primary_artist(album_data.get("artists", []))

        items:  list[dict] = []
        offset: int        = 0
        limit:  int        = 50

        while True:
            page = cls._api_get(
                f"/albums/{album_id}/tracks",
                params={"limit": limit, "offset": offset},
            )
            for track in page.get("items", []):
                if track is None:
                    continue
                d = cls._track_dict_from_album_track(
                    track,
                    artist_fallback=artist,
                    album_art=album_art,
                    album_name=album_name,
                )
                items.append(d)
                if on_item:
                    on_item(d)

            if not page.get("next"):
                break
            offset += limit

        return items

    @classmethod
    def _resolve_playlist_api(
        cls,
        playlist_id: str,
        on_item:     Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Fetch all tracks in a playlist, paginated in groups of 100.
        Handles ``null`` track entries (deleted items) gracefully.
        """
        items:  list[dict] = []
        offset: int        = 0
        limit:  int        = 100

        while True:
            page = cls._api_get(
                f"/playlists/{playlist_id}/tracks",
                params={
                    "limit":  limit,
                    "offset": offset,
                    "fields": (
                        "next,items(track(id,name,duration_ms,"
                        "artists,album(name,images)))"
                    ),
                },
            )
            for item in page.get("items", []):
                track = (item or {}).get("track")
                if not track:
                    continue
                d = cls._track_dict_from_api(track)
                items.append(d)
                if on_item:
                    on_item(d)

            if not page.get("next"):
                break
            offset += limit

        return items

    @classmethod
    def _resolve_artist_api(
        cls,
        artist_id: str,
        on_item:   Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Fetch the complete discography of an artist.

        Strategy:
        1. Fetch all album+single IDs via ``/v1/artists/{id}/albums``
           (paginated, 50 per page).
        2. For each album, call ``_resolve_album_api()`` with the same
           ``on_item`` callback so the UI populates progressively.

        Note: compilations and appears_on are excluded to keep the list
        focused on the artist's own releases.
        """
        all_items: list[dict] = []
        offset:    int        = 0
        limit:     int        = 50

        # Step 1: collect all album IDs
        album_ids: list[str] = []
        while True:
            page = cls._api_get(
                f"/artists/{artist_id}/albums",
                params={
                    "include_groups": "album,single",
                    "limit":          limit,
                    "offset":         offset,
                    "market":         "US",
                },
            )
            for album in page.get("items", []):
                if album and album.get("id"):
                    album_ids.append(album["id"])

            if not page.get("next"):
                break
            offset += limit

        # Step 2: resolve each album's tracks progressively
        for album_id in album_ids:
            tracks = cls._resolve_album_api(album_id, on_item=on_item)
            all_items.extend(tracks)

        return all_items

    # ──────────────────────────────────────────────────────────────────────────
    # Embed API fallback (no credentials required)
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _embed_fallback(
        cls,
        entity_type: str,
        entity_id:   str,
        on_item:     Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Fetch the Spotify embed page and parse the ``__NEXT_DATA__`` payload.
        Works for track / album / playlist without any credentials.
        Fragile – Spotify may change the embed payload structure at any time.
        """
        embed_url = f"{_EMBED_BASE}/{entity_type}/{entity_id}"
        req = urllib.request.Request(embed_url, headers={"User-Agent": _REQUEST_UA})

        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch Spotify embed page: {exc}") from exc

        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not m:
            raise RuntimeError(
                "Could not find internal data in Spotify embed page. "
                "Spotify may have changed their embed structure."
            )

        try:
            data   = json.loads(m.group(1))
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        except Exception as exc:
            raise RuntimeError(f"Error parsing Spotify embed JSON: {exc}") from exc

        items: list[dict] = []

        def _ms(track_obj: dict) -> int:
            d = track_obj.get("duration")
            if isinstance(d, dict):
                return int(d.get("totalMilliseconds", 0))
            if isinstance(d, (int, float)):
                return int(d)
            return int(track_obj.get("duration_ms", 0))

        if entity_type == "track":
            title  = entity.get("name") or "Unknown Title"
            artist = entity.get("subtitle") or "Unknown Artist"
            ms     = _ms(entity)
            d = cls._make_dict(title, artist, ms, "", f"https://open.spotify.com/track/{entity_id}")
            items.append(d)
            if on_item:
                on_item(d)

        elif entity_type in ("album", "playlist"):
            for track in entity.get("trackList", []):
                title  = track.get("title") or "Unknown Title"
                artist = track.get("subtitle") or "Unknown Artist"
                ms     = _ms(track)
                uid    = track.get("uid") or ""
                spotify_url = f"https://open.spotify.com/track/{uid}" if uid else ""
                d = cls._make_dict(title, artist, ms, "", spotify_url)
                items.append(d)
                if on_item:
                    on_item(d)

        if not items:
            raise RuntimeError("No actionable tracks found in this Spotify link.")

        return items

    @classmethod
    def _resolve_proxy(
        cls,
        url:          str,
        proxy_base:   str,
        proxy_token:  str,
        on_item:      Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Resolve a Spotify URL via the configured proxy server.
        Endpoint: /api/v1/resolve?url={url}
        Header: X-App-Token
        """
        proxy_base = proxy_base.rstrip("/")
        endpoint   = f"{proxy_base}/api/v1/resolve"
        params     = {"url": url}
        headers    = {"User-Agent": _REQUEST_UA}
        if proxy_token:
            headers["X-App-Token"] = proxy_token

        full_url = f"{endpoint}?{urllib.parse.urlencode(params)}"
        logger.debug(f"Proxy Fetching: {full_url}")
        # Set dynamic timeout: artists can take much longer (up to 10 mins)
        match_type = re.search(r"open\.spotify\.com/(track|album|playlist|artist)/", url)
        entity_type = match_type.group(1) if match_type else "track"
        timeout = 600 if entity_type == "artist" else 120
        req = urllib.request.Request(full_url, headers=headers)
        
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                logger.debug(f"Proxy Status Code: {resp.status}")
                raw_data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as he:
            if he.code == 429:
                msg = "Spotify Rate Limit exceeded (Too Many Requests). Please wait a few minutes and try again."
                logger.error(f"[SpotifyResolver] {msg}")
                raise RuntimeError(msg) from he
            logger.error(f"[SpotifyResolver] Proxy HTTP ERROR: {he.code} {he.reason}")
            raise RuntimeError(f"Proxy resolution failed (HTTP {he.code}): {he.reason}") from he
        except Exception as exc:
            logger.error(f"[SpotifyResolver] Proxy ERROR: {exc}")
            raise RuntimeError(f"Proxy resolution failed: {exc}") from exc

        # New format: {"status": "success", "data": {"metadata": {...}, "items": [...]}}
        data = raw_data.get("data") if isinstance(raw_data, dict) else raw_data
        
        # Support multiple possible keys for the main track list
        items = []
        if isinstance(data, dict):
            # 1. Direct 'items' or 'results' or 'tracks'
            potential_items = data.get("items") or data.get("results") or data.get("tracks") or data.get("data")
            
            if isinstance(potential_items, list):
                items = potential_items
            elif isinstance(potential_items, dict):
                # 2. Nested: { tracks: { items: [...] } }
                items = potential_items.get("items") or potential_items.get("results") or []
        elif isinstance(data, list):
            items = data

        if not items and isinstance(data, dict) and "title" in data:
            # Fallback for single track or unknown flat format
            items = [data]

        metadata = (data.get("metadata") or data) if isinstance(data, dict) else {}
        
        # Smart parent detection: For albums, prefer the 'artist' field over the 'name' (which is the album title)
        is_album = metadata.get("type") == "album" or entity_type == "album"
        parent_artist_name = ""
        if is_album:
            parent_artist_name = metadata.get("artist") or metadata.get("name") or ""
        else:
            parent_artist_name = metadata.get("name") or metadata.get("owner") or metadata.get("artist") or ""

        # Ensure all items are normalized and collected
        normalized_list: list[dict] = []
        for item in items:
            # Reconstruct the expected dict format if keys are different
            # Server returns: title, artist, yt_query, image_url, album, duration_sec
            normalized = {
                "title":         item.get("title") or item.get("name") or "Unknown Title",
                "artist":        item.get("artist") or item.get("author") or "Unknown Artist",
                "url":           item.get("yt_query") or item.get("url") or item.get("spotify_url") or "",
                "duration_sec":  item.get("duration_sec") or item.get("duration"),
                "thumbnail_url": item.get("image_url") or item.get("thumbnail_url") or "",
                "spotify_url":   item.get("spotify_url") or url or "",
                "album":         item.get("album") or item.get("album_name") or "",
                "album_type":    item.get("album_type") or item.get("type", "album"),
                "release_type":  item.get("album_type") or item.get("type", "album"),
                "album_index":   item.get("track_number") or item.get("index") or 0,
                "parent_artist": parent_artist_name,
            }
            normalized_list.append(normalized)
            if on_item:
                on_item(normalized)

        return normalized_list

    @classmethod
    def _resolve_artist_ytm_fallback(
        cls,
        spotify_url: str,
        artist_id: str,
        on_item: Optional[Callable[[dict], None]] = None,
    ) -> list[dict]:
        """
        Fallback: Resolve artist discography using YouTube Music when Spotify is rate-limited.
        1. Get artist name from Spotify Embed (public).
        2. resolve_artist_via_ytm using that name.
        """
        # Step 1: Scrape the artist name from the public page
        artist_name = "Unknown Artist"
        try:
            req = urllib.request.Request(spotify_url, headers={"User-Agent": _REQUEST_UA, "Accept-Language": "he-IL"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")
                artist_name = cls._extract_artist_name_from_html(html)
        except Exception as exc:
            logger.error(f"[SpotifyResolver] Failed to public-scrape artist name: {exc}")

        if artist_name == "Unknown Artist":
            # If we still don't have a name, we can't search YTM safely
            raise RuntimeError("Spotify Rate Limited and could not resolve artist name for fallback.")

        logger.info(f"[SpotifyResolver] Falling back to YTM discography for: {artist_name}")
        
        # Step 2: Use the YTM scraper logic (we should ideally have a shared utility for this)
        # For now, we utilize ytmusicapi which is already a dependency of the downloader
        try:
            from ytmusicapi import YTMusic
            ytm = YTMusic()
            
            # Search for the artist
            search = ytm.search(artist_name, filter="artists")
            if not search:
                return []
            
            ytm_artist_id = search[0]["browseId"]
            actual_artist_name = search[0].get("artist") or artist_name
            
            artist_data = ytm.get_artist(ytm_artist_id)
            all_tracks = []
            release_items = []

            # Categories to pull
            for key in ["albums", "singles"]:
                section = artist_data.get(key, {})
                params = section.get("params")
                items = []
                if params:
                    try:
                        items = ytm.get_artist_albums(ytm_artist_id, params)
                    except Exception:
                        items = section.get("results", [])
                else:
                    items = section.get("results", [])
                
                for itm in items:
                    if "browseId" in itm:
                        release_items.append((itm["browseId"], itm.get("title")))

            # Normalize to Spotify-like format
            from utils.artwork_cleaner import clean_artwork_url
            from core.playlist_parser import SourcePlatform

            for alb_id, alb_title in release_items:
                try:
                    album = ytm.get_album(alb_id)
                    raw_image = album.get("thumbnails", [{}])[-1].get("url", "")
                    image_url = clean_artwork_url(raw_image, SourcePlatform.YOUTUBE_MUSIC)
                    
                    for idx, t in enumerate(album.get("tracks", [])):
                        normalized = {
                            "title":         t["title"],
                            "artist":        actual_artist_name,
                            "url":           f"ytsearch1:{actual_artist_name} {t['title']} audio",
                            "duration_sec":  t.get("duration_seconds"),
                            "thumbnail_url": image_url,
                            "spotify_url":   "", # No Spotify URL for YTM items
                            "album":         alb_title,
                            "album_type":    "album",
                            "release_type":  "album",
                            "album_index":   idx + 1,
                            "parent_artist": actual_artist_name,
                        }
                        all_tracks.append(normalized)
                        if on_item:
                            on_item(normalized)
                    time.sleep(0.05) # small delay
                except Exception:
                    continue

            return all_tracks
        except Exception as exc:
            logger.error(f"[SpotifyResolver] YTM Fallback failed: {exc}")
            raise RuntimeError(f"Spotify Rate Limited and YTM fallback failed: {exc}") from exc

    # ──────────────────────────────────────────────────────────────────────────
    # Spotify Web API transport
    # ──────────────────────────────────────────────────────────────────────────

    @classmethod
    def _get_access_token(cls) -> str:
        """
        Return a valid access token, refreshing if expired.
        Uses the client_credentials flow (no user login required).
        """
        # Return cached token if still valid (with a 30-second safety margin)
        with cls._token_lock:
            if cls._token and time.time() < (cls._token_expiry - 30):
                return cls._token

        client_id, client_secret = cls._get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Spotify API credentials are not configured.  "
                "Set spotify_client_id and spotify_client_secret in Settings → Spotify."
            )

        credentials  = f"{client_id}:{client_secret}"
        encoded_creds = base64.b64encode(credentials.encode()).decode()

        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req  = urllib.request.Request(
            _TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {encoded_creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
                "User-Agent":    _REQUEST_UA,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                token_data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"Spotify token request failed (HTTP {exc.code}): {body_text}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"Spotify token request error: {exc}") from exc

        with cls._token_lock:
            cls._token        = token_data.get("access_token", "")
            expires_in        = int(token_data.get("expires_in", 3600))
            cls._token_expiry = time.time() + expires_in

            if not cls._token:
                raise RuntimeError("Spotify returned an empty access token.")

            return cls._token

    @classmethod
    def _api_get(cls, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        Perform an authenticated GET request to the Spotify Web API.
        Handles rate-limiting (HTTP 429) with a single 1-second retry.
        """
        token = cls._get_access_token()
        url   = _API_BASE + endpoint
        if params:
            url += "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept":        "application/json",
                "User-Agent":    _REQUEST_UA,
            },
        )

        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 0:
                    # Honour Retry-After header if present, otherwise wait 1s
                    retry_after = int(exc.headers.get("Retry-After", "1"))
                    time.sleep(min(retry_after, 5))
                    # Refresh token header for the retry
                    req.add_header("Authorization", f"Bearer {cls._get_access_token()}")
                    continue
                if exc.code == 401:
                    # Token expired mid-flight – clear cache and retry once
                    with cls._token_lock:
                        cls._token = ""
                        cls._token_expiry = 0.0
                    if attempt == 0:
                        token = cls._get_access_token()
                        req.add_header("Authorization", f"Bearer {token}")
                        continue
                raise RuntimeError(
                    f"Spotify API error {exc.code} for {endpoint}"
                ) from exc
            except Exception as exc:
                raise RuntimeError(f"Spotify API request failed: {exc}") from exc

        raise RuntimeError(f"Spotify API request failed after retries: {endpoint}")

    # ──────────────────────────────────────────────────────────────────────────
    # Dict builders
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _make_dict(
        title:        str,
        artist:       str,
        duration_ms:  int,
        thumbnail_url: str,
        spotify_url:  str,
        album_type:   str = "single",
    ) -> dict:
        """Build the standard output dict for one resolved track."""
        duration_sec = int(duration_ms / 1000) if duration_ms else None
        search_query = f"ytsearch1:{artist} {title} audio"
        return {
            "title":         title,
            "artist":        artist,
            "url":           search_query,
            "duration_sec":  duration_sec,
            "thumbnail_url": thumbnail_url,
            "spotify_url":   spotify_url,
            "album_type":    album_type,
        }

    @classmethod
    def _track_dict_from_api(cls, track: dict) -> dict:
        """Convert a full Spotify Web API track object into our output dict."""
        title       = track.get("name") or "Unknown Title"
        artist      = cls._primary_artist(track.get("artists", []))
        duration_ms = int(track.get("duration_ms", 0))
        spotify_url = (track.get("external_urls") or {}).get("spotify", "")

        # Artwork is nested inside album object for track endpoints
        album      = track.get("album") or {}
        images     = album.get("images") or []
        thumb      = cls._best_image(images)
        a_type     = album.get("album_type", "single")

        return cls._make_dict(title, artist, duration_ms, thumb, spotify_url, album_type=a_type)

    @classmethod
    def _track_dict_from_album_track(
        cls,
        track:          dict,
        artist_fallback: str = "",
        album_art:       str = "",
        album_name:      str = "",
    ) -> dict:
        """
        Convert a simplified track object (as returned by the album tracks
        endpoint) into our output dict.  Album-level fields come from the
        caller since the per-track objects don't include artwork.
        """
        title       = track.get("name") or "Unknown Title"
        # Per-track artists override album-level artist
        artist      = cls._primary_artist(track.get("artists", [])) or artist_fallback
        duration_ms = int(track.get("duration_ms", 0))
        spotify_url = (track.get("external_urls") or {}).get("spotify", "")

        return cls._make_dict(title, artist, duration_ms, album_art, spotify_url, album_type="album")

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _primary_artist(artists: list) -> str:
        """Return the name of the first artist in the list, or empty string."""
        if artists and isinstance(artists[0], dict):
            return artists[0].get("name", "")
        return ""

    @staticmethod
    def _best_image(images: list) -> str:
        """
        Pick the highest-resolution image URL from a Spotify images list.
        Spotify images are usually sorted largest-first, but we sort explicitly
        by width (descending) for safety.
        """
        if not images:
            return ""
        valid = [i for i in images if i and i.get("url")]
        if not valid:
            return ""
        # Sort by width descending; images without width sort last
        sorted_imgs = sorted(valid, key=lambda i: i.get("width", 0), reverse=True)
        return sorted_imgs[0]["url"]

    @staticmethod
    def _get_credentials() -> tuple[str, str]:
        """
        Read Spotify credentials from AppConfig.
        Returns (client_id, client_secret) – both empty strings when not set.
        Importing AppConfig here (instead of at module level) avoids a circular
        import since config.py has no dependency on utils/.
        """
        try:
            from config import AppConfig
            cfg = AppConfig()
            return cfg.spotify_client_id.strip(), cfg.spotify_client_secret.strip()
        except Exception:
            return "", ""

    @staticmethod
    def _get_proxy_config() -> tuple[str, str]:
        """Read proxy configuration from AppConfig."""
        try:
            from config import AppConfig
            cfg = AppConfig()
            return cfg.proxy_server_url.strip(), cfg.spotify_app_api_key.strip()
        except Exception:
            return "", ""

    @staticmethod
    def _extract_artist_name_from_html(html: str) -> str:
        """Helper to parse artist name from Spotify HTML."""
        # Look for <title>Artist Name | Spotify</title> or similar
        m = re.search(r"<title>\s*(.*?)\s*\|\s*Spotify\s*</title>", html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        
        # Alternative: meta og:title
        m2 = re.search(r'property=["\']og:title["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
        if m2:
            return m2.group(1).strip()
            
        return "Unknown Artist"
