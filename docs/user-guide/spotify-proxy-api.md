# Spotify Search Proxy API v1

Status: **Current / normative contract for optional Spotify text search**

This API is used only for **Spotify text search in BananaFlow's Search panel**. It is not required to paste/import a Spotify track, album, playlist or artist URL; URL import uses BananaFlow's Spotify scraper/resolution path.

## Endpoint

```http
GET /api/v1/search?query=<url-encoded query>&limit=<1-100>
```

### Request

Required query parameters:

- `query` — non-empty search text.
- `limit` — requested maximum number of results; the server may return fewer.

Optional request header:

```http
X-App-Token: <configured token>
```

BananaFlow sends the token only when one is configured. Use HTTPS for a remote/production proxy.

## Canonical response

The preferred v1 response is categorized and wrapped in `data`:

```json
{
  "data": {
    "tracks": [
      {
        "title": "Example Song",
        "artist": "Example Artist",
        "duration_sec": 229,
        "thumbnail_url": "https://i.scdn.co/image/...",
        "url": "https://open.spotify.com/track/EXAMPLE"
      }
    ],
    "albums": [],
    "artists": [],
    "playlists": []
  }
}
```

Each category is optional. Empty categories may be omitted or returned as empty arrays.

### Common fields

| Field | Required | Meaning |
|---|---|---|
| `title` or `name` | yes | Display name/title |
| `artist` | recommended for tracks/albums | Primary/combined artist display name |
| `duration_sec` | optional | Duration in whole seconds |
| `thumbnail_url` or `image_url` | optional | Artwork URL |
| `url` or `spotify_url` | recommended | Canonical Spotify web URL |
| `id` / `uri` | optional fallback | Used to reconstruct a Spotify URL when `url` is absent |
| `album` / `album_name` | optional | Album display value |
| `album_type` | optional | e.g. album/single/compilation |
| `total_tracks` / `item_count` | optional | Album/playlist count |

For an artist item, `title`/`name` is also used as its artist display name when `artist` is absent.

## Legacy-compatible response forms

The current client accepts two compatibility forms in addition to the canonical response:

```json
{"data":{"results":[{"type":"track","title":"..."}]}}
```

or a top-level payload without the `data` wrapper. These are compatibility inputs, not the recommended schema for a new server.

Do not create a second unofficial v1 schema in new documentation or examples.

## Errors

Use conventional HTTP statuses with a small JSON error body:

```json
{"error":"human-readable summary"}
```

Recommended statuses:

- `400` — missing/invalid `query` or `limit`;
- `401`/`403` — proxy authentication rejected;
- `429` — proxy/upstream rate limit;
- `502`/`503` — Spotify/upstream unavailable;
- `500` — unexpected proxy error.

The parameter name is **`query`**, not `q`.

## Minimal Flask example

```python
import os
from flask import Flask, jsonify, request
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

app = Flask(__name__)
sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=os.environ["SPOTIFY_CLIENT_ID"],
    client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
))

@app.get("/api/v1/search")
def search():
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify(error="Missing query parameter 'query'"), 400
    try:
        limit = max(1, min(100, int(request.args.get("limit", "15"))))
    except ValueError:
        return jsonify(error="Invalid limit"), 400

    items = sp.search(q=query, type="track", limit=limit)["tracks"]["items"]
    tracks = [{
        "title": item["name"],
        "artist": ", ".join(a["name"] for a in item.get("artists", [])),
        "duration_sec": item.get("duration_ms", 0) // 1000,
        "thumbnail_url": (item.get("album", {}).get("images") or [{}])[0].get("url", ""),
        "url": item.get("external_urls", {}).get("spotify", ""),
    } for item in items]
    return jsonify(data={"tracks": tracks})
```

This example is intentionally minimal; production deployments still need authentication policy, secret management, rate limiting, timeouts, safe logging and TLS.

## Security / privacy

- Never put Spotify client secrets or proxy tokens in this repository or client-distributed code.
- Store server-side credentials in the server's secret/environment mechanism.
- Use HTTPS for remote deployments.
- Do not log `X-App-Token` or other credentials.
- Validate limits/input and apply rate limits.
- A self-hosted/custom proxy is a separate operator: users should trust it before sending search queries/tokens.

## Client behavior

BananaFlow calls `<proxy>/api/v1/search` with `query` and `limit`, parses the categorized payload, and converts track hits into separate YouTube-source resolution/download requests. Non-track results retain their Spotify URL so they can be expanded/imported by the normal Spotify path.
