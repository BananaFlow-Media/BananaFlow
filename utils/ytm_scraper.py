import requests
import re
import os
from typing import List, Dict, Optional
from dotenv import load_dotenv
from utils.yt_dlp_opts import CHROME_USER_AGENT
from utils.logger import SilentLogger

load_dotenv()

_API_BASE = "https://music.youtube.com/youtubei/v1/browse"


def _api_url() -> str:
    """Build the endpoint without retaining credentials in module globals."""
    from urllib.parse import urlencode

    api_key = (
        os.environ.get("BANANAFLOW_YOUTUBE_API_KEY", "").strip()
        or os.environ.get("YOUTUBE_API_KEY", "").strip()
    )
    query = {"prettyPrint": "false"}
    if api_key:
        query["key"] = api_key
    return f"{_API_BASE}?{urlencode(query)}"
_CLIENT_CONTEXT = {
    "client": {
        "hl": "iw", "gl": "IL", "clientName": "WEB_REMIX",
        "clientVersion": "1.20260331.01.00", "osName": "Windows",
        "osVersion": "10.0", "platform": "DESKTOP"
    }
}

def _clean_name(name: str) -> str:
    if not name: return ""
    for pattern in [r" - הערוץ הרשמי$", r" - Official Channel$", r" - Official$", r" - Topic$", r"^Album - "]:
        name = re.sub(pattern, "", name, flags=re.I)
    return name.strip()

def _call_api(browse_id: Optional[str] = None, params: Optional[str] = None, continuation: Optional[str] = None, click_tracking: Optional[str] = None, visitor_data: Optional[str] = None) -> Optional[dict]:
    # We must deep-copy context to avoid polluting the global _CLIENT_CONTEXT
    import copy
    payload = {"context": copy.deepcopy(_CLIENT_CONTEXT)}
    if visitor_data:
        payload["context"]["client"]["visitorData"] = visitor_data
        
    if continuation:
        payload["continuation"] = continuation
        if click_tracking:
            payload["clickTrackingParams"] = click_tracking
    else:
        payload["browseId"] = browse_id
        if params:
            payload["params"] = params
    try:
        r = requests.post(_api_url(), json=payload,
                          headers={"Content-Type": "application/json", "User-Agent": CHROME_USER_AGENT},
                          timeout=20)
        return r.json()
    except Exception:
        return None

def _resolve_ytm_artist_id(artist_url: str) -> Optional[str]:
    """Return the UC browse id needed by the YouTube Music browse API."""
    for marker in ("/channel/", "/browse/"):
        if marker in artist_url:
            artist_id = artist_url.split(marker, 1)[1].split("?", 1)[0].split("/", 1)[0]
            return artist_id or None

    if "/@" not in artist_url:
        return None

    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "logger": SilentLogger(),
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(artist_url, download=False)
    except Exception:
        return None

    for key in ("channel_id", "uploader_id", "id"):
        value = (info or {}).get(key)
        if isinstance(value, str) and value.startswith("UC"):
            return value

    channel_url = (info or {}).get("channel_url") or (info or {}).get("uploader_url") or ""
    match = re.search(r"/(?:channel|browse)/(UC[A-Za-z0-9_-]+)", channel_url)
    if match:
        return match.group(1)

    return None

def _extract_items(data: dict, artist_name: str, item_type: str) -> List[Dict]:
    """Walk entire JSON tree and collect all music item renderers."""
    results = []
    seen = set()

    def _walk(obj):
        if isinstance(obj, dict):
            renderer = (obj.get("musicTwoRowItemRenderer") or
                        obj.get("musicResponsiveListItemRenderer") or
                        obj.get("musicVideoRenderer"))
            if renderer:
                # Get title
                title = ""
                runs = renderer.get("title", {}).get("runs", [])
                if runs:
                    title = "".join(r.get("text", "") for r in runs)
                if not title:
                    cols = renderer.get("flexColumns", [])
                    if cols:
                        title = cols[0].get("musicResponsiveListItemFlexColumnRenderer", {})\
                                       .get("text", {}).get("runs", [{}])[0].get("text", "")
                title = _clean_name(title)

                # 1. Try to find a Playlist ID (Best for Albums/Singles)
                playlist_id = None
                overlay = renderer.get("thumbnailOverlay", {}) or renderer.get("overlay", {})
                if isinstance(overlay, dict):
                    play_nav = (overlay.get("musicItemThumbnailOverlayRenderer", {})\
                                       .get("content", {})\
                                       .get("musicPlayButtonRenderer", {})\
                                       .get("playNavigationEndpoint", {}) or {})
                    playlist_id = play_nav.get("watchPlaylistEndpoint", {}).get("playlistId")

                # 2. Try to find a Video ID & Browse ID with nested fallbacks
                nav = renderer.get("navigationEndpoint") or {}
                video_id = (nav.get("watchEndpoint", {}).get("videoId") or
                            nav.get("watchPlaylistEndpoint", {}).get("videoId"))
                browse_id = nav.get("browseEndpoint", {}).get("browseId")

                # Fallback: Check flexColumns[0] (Standard for musicResponsiveListItemRenderer)
                if not video_id and not browse_id and not playlist_id:
                    cols = renderer.get("flexColumns", [])
                    if cols:
                        col_nav = (cols[0].get("musicResponsiveListItemFlexColumnRenderer", {}) \
                                          .get("text", {}).get("runs", [{}])[0] \
                                          .get("navigationEndpoint", {}) or {})
                        video_id = col_nav.get("watchEndpoint", {}).get("videoId")
                        browse_id = col_nav.get("browseEndpoint", {}).get("browseId")
                        playlist_id = col_nav.get("watchPlaylistEndpoint", {}).get("playlistId")

                # Fallback: Main Nav (Playlist support)
                if not playlist_id:
                    playlist_id = nav.get("watchPlaylistEndpoint", {}).get("playlistId")

                # Decision Logic:
                # - If it's an album/single, we prefer playlist_id to get ALL tracks.
                # - If its a performance/video, we prefer video_id for a direct link.
                if item_type in ("album", "single") and playlist_id:
                    final_id = playlist_id
                    final_url = f"https://www.youtube.com/playlist?list={playlist_id}"
                elif video_id:
                    final_id = video_id
                    final_url = f"https://www.youtube.com/watch?v={video_id}"
                elif browse_id:
                    final_id = browse_id
                    # Clean up Browse ID (don't use it for videos/performances if possible)
                    if item_type in ("performance", "video") and not browse_id.startswith("UC"):
                         return # Skip weird browse IDs for individual tracks
                    final_url = f"https://music.youtube.com/browse/{browse_id}"
                else:
                    return

                # 3. Extract thumbnail
                thumb_url = ""
                thumbnails = renderer.get("thumbnail", {}).get("thumbnails", [])
                if thumbnails:
                    # Prefer the largest available thumbnail
                    thumb_url = thumbnails[-1].get("url", "")

                if final_id and title and final_id not in seen:
                    seen.add(final_id)
                    results.append({
                        "id": final_id,
                        "url": final_url,
                        "title": title,
                        "thumbnail_url": thumb_url,
                        "type": item_type,
                        "artist": artist_name,
                        "parent_artist": artist_name,
                        "album": title if item_type == "album" else "",
                    })
                return

            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for i in obj:
                _walk(i)

    _walk(data)
    return results

def _find_continuation(data) -> Optional[str]:
    """Find next-page continuation token.

    KEY INSIGHT: Always recurse into 'contents' BEFORE checking 'continuations'
    at the same level. This ensures we find musicPlaylistShelfRenderer.continuations
    (which paginates ITEMS) instead of sectionListRenderer.continuations
    (which paginates SECTIONS and returns no new music items).
    """
    if isinstance(data, list):
        for item in data:
            r = _find_continuation(item)
            if r:
                return r
        return None

    if not isinstance(data, dict):
        return None

    # 1. Direct hit at this level
    if "nextContinuationData" in data:
        return data["nextContinuationData"].get("continuation")

    # 2. Recurse into 'contents' FIRST (music items live here)
    if "contents" in data:
        r = _find_continuation(data["contents"])
        if r:
            return r

    # 3. NOW check 'continuations' at this level
    conts = data.get("continuations")
    if isinstance(conts, list) and conts:
        c = conts[0]
        if isinstance(c, dict) and "nextContinuationData" in c:
            return c["nextContinuationData"].get("continuation")

    # 4. Recurse into remaining keys (skip already-processed ones)
    for key, val in data.items():
        if key in ("contents", "continuations"):
            continue
        r = _find_continuation(val)
        if r:
            return r

    return None

def _extract_continuation_info(data: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (continuation_token, clickTrackingParams) if present in data."""
    token = None
    click = None
    def _walk(obj):
        nonlocal token, click
        if isinstance(obj, dict):
            if "nextContinuationData" in obj:
                nd = obj["nextContinuationData"]
                token = nd.get("continuation")
                click = nd.get("clickTrackingParams")
                return
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for i in obj:
                _walk(i)
    _walk(data)
    return token, click

def _drain_shelf(browse_id: str, params: Optional[str], artist_name: str, item_type: str, visitor_data: Optional[str] = None) -> List[Dict]:
    """Fetch ALL items from a See-All page (MPAD, VLOLAK, UC+params) by following continuations."""
    items = []
    seen_tokens = set()

    data = _call_api(browse_id, params=params, visitor_data=visitor_data)
    while data:
        page_items = _extract_items(data, artist_name, item_type)
        if not page_items:
            break
        items.extend(page_items)

        token, click = _extract_continuation_info(data)
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
        data = _call_api(continuation=token, click_tracking=click, visitor_data=visitor_data)

    return items

def fetch_ytm_artist_releases(artist_url: str) -> List[Dict]:
    # Parse artist ID
    artist_id = _resolve_ytm_artist_id(artist_url)
    if not artist_id:
        return []

    print(f"[YTM] Fetching artist page: {artist_id}")
    main_data = _call_api(artist_id)
    if not main_data:
        return []

    # Extract dynamic visitorData from response context to maintain session state
    visitor_data = main_data.get("responseContext", {}).get("visitorData")

    # Extract artist name from header
    header = main_data.get("header", {})
    hdr_r = header.get("musicVisualHeaderRenderer") or header.get("musicImmersiveHeaderRenderer") or {}
    artist_name = _clean_name(hdr_r.get("title", {}).get("runs", [{}])[0].get("text", "Unknown Artist"))

    all_results = []       # Final list
    added_ids_per_type = {}  # type -> set(id)  –  deduplicate within each category only

    def _add_items(items: List[Dict]):
        for item in items:
            t = item["type"]
            # Opt-out of generic "video" shelf when downloading full artist
            if t == "video":
                continue
            if item["id"] not in added_ids_per_type.get(t, set()):
                added_ids_per_type.setdefault(t, set()).add(item["id"])
                all_results.append(item)

    # ── Step 1: Walk the main page and collect every shelf ─────────────────────
    # Map Hebrew shelf title → internal type
    SHELF_TYPES = {
        "אלבומים": "album",
        "סינגלים": "single",
        "סרטונים": "video",
        "הופעות": "performance",
        "Live": "performance",
    }

    def _shelf_type(text: str) -> Optional[str]:
        for key, val in SHELF_TYPES.items():
            if key in text:
                return val
        return None

    shelves: List[tuple] = []   # (item_type, shelf_renderer, label)
    bonus_playlists: List[tuple] = []  # (item_type, playlist_id, label)

    def _collect_sources(obj):
        if isinstance(obj, dict):
            # Standard carousel shelf
            if "musicCarouselShelfRenderer" in obj:
                shelf = obj["musicCarouselShelfRenderer"]
                h_runs = shelf.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})\
                              .get("title", {}).get("runs", [])
                label = "".join(r.get("text", "") for r in h_runs).strip()
                s_type = _shelf_type(label)
                if s_type:
                    shelves.append((s_type, shelf, label))
                    return  # Don't recurse deeper into the shelf here; we'll process it separately

            for v in obj.values():
                _collect_sources(v)
        elif isinstance(obj, list):
            for i in obj:
                _collect_sources(i)

    _collect_sources(main_data)

    # ── Step 2: Process each shelf (Albums → Singles → Performances → Videos) ──
    TYPE_ORDER = ["album", "single", "performance", "video"]
    shelves.sort(key=lambda x: TYPE_ORDER.index(x[0]) if x[0] in TYPE_ORDER else 99)

    for s_type, shelf, label in shelves:
        print(f"[YTM]  Shelf: {label}")

        # Immediate items on the main page carousel
        immediate = _extract_items(shelf, artist_name, s_type)
        _add_items(immediate)

        # Follow "See All" button if present
        btn = shelf.get("header", {}).get("musicCarouselShelfBasicHeaderRenderer", {})\
                    .get("moreContentButton", {})
        eb = btn.get("buttonRenderer", {}).get("navigationEndpoint", {}).get("browseEndpoint", {})
        if eb and eb.get("browseId"):
            see_all_id = eb["browseId"]
            see_all_params = eb.get("params")
            full_items = _drain_shelf(see_all_id, see_all_params, artist_name, s_type, visitor_data)
            if full_items:
                _add_items(full_items)
                print(f"[YTM]    -> See-All gave {len(full_items)} items")
            else:
                _add_items(immediate)
                print(f"[YTM]    -> See-All gave 0 items, falling back to {len(immediate)} carousel items")
        else:
            print(f"[YTM]    -> No See-All (using {len(immediate)} carousel items)")

    # ── Step 3: Removed bonus playlists per user request ──────────

    counts = {}
    for it in all_results:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    print(f"[YTM] Total: {len(all_results)} items – {counts}")

    return all_results
