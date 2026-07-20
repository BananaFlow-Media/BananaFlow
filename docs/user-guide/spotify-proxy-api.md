# Spotify Proxy Server API Contract

This document describes the API that your Spotify proxy server must implement for BananaFlow to search Spotify.

## Overview

Instead of connecting directly to Spotify, the BananaFlow client sends search requests to **your** proxy server. The proxy server handles authentication with Spotify and returns results to the client. This allows users to search Spotify without needing their own Spotify developer credentials.

## Configuration

### Client Side (BananaFlow)

1. Go to **Settings** → **Spotify Proxy Server**
2. Enter your proxy server URL: `http://your-server.com` (with or without port)
3. If the proxy requires authentication, enter its token in **App API Key**
4. Leave the server URL empty to disable Spotify search

The app ships with no proxy token or Spotify credential. Keep tokens out of
source control. For managed deployments, set `BANANAFLOW_SPOTIFY_APP_API_KEY`
instead of writing the token to a shared configuration file.

Example URLs:
- `http://localhost:5000` (local development)
- `https://spotify.myapp.com` (production)
- `http://192.168.1.100:8080` (custom port)

### Server Side (Your Proxy Implementation)

Implement these **GET** endpoints:

- `GET /api/v1/search?query={query}&limit={count}`
- `GET /api/v1/resolve?url={spotify_url}` for Spotify URL imports

## API Endpoint

### Request

```
GET /api/v1/search?query=example+artist+song&limit=15
```

**Query Parameters:**
- `query` (required): URL-encoded search query (e.g., "artist name song title")
- `limit` (required): Number of results to return (1–50)

**Headers (from client):**
```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...
Accept: application/json
X-App-Token: <configured proxy token, when required>
```

### Response

**Status: 200 OK**

```json
{
  "results": [
    {
      "title": "Example Song One",
      "artist": "Example Artist",
      "duration_sec": 229,
      "thumbnail_url": "https://i.scdn.co/image/...",
      "url": "https://open.spotify.com/track/EXAMPLE_TRACK_ID_1"
    },
    {
      "title": "Example Song Two",
      "artist": "Example Artist",
      "duration_sec": 178,
      "thumbnail_url": "https://i.scdn.co/image/...",
      "url": "https://open.spotify.com/track/EXAMPLE_TRACK_ID_2"
    }
  ]
}
```

**Field Descriptions:**
- `title` (string): Track name
- `artist` (string): Primary artist name
- `duration_sec` (int or null): Track duration in seconds (optional)
- `thumbnail_url` (string): Spotify album cover URL (optional, empty string if unavailable)
- `url` (string): Full Spotify track URL (e.g., `https://open.spotify.com/track/...`)

### Error Responses

**Status: 400 Bad Request** – Missing or invalid query parameter
```json
{
  "error": "Missing query parameter 'q'"
}
```

**Status: 401 Unauthorized** – Spotify authentication failed
```json
{
  "error": "Spotify authentication failed: invalid credentials"
}
```

**Status: 503 Service Unavailable** – Spotify API down
```json
{
  "error": "Spotify API is unavailable"
}
```

**Status: 500 Internal Server Error** – Server error
```json
{
  "error": "Internal server error: Database connection failed"
}
```

## Implementation Examples

### Python (Flask)

```python
from flask import Flask, request, jsonify
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

app = Flask(__name__)

# Spotify credentials (from environment variables)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

auth_manager = SpotifyClientCredentials(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET
)
sp = spotipy.Spotify(auth_manager=auth_manager)

@app.route("/api/v1/search", methods=["GET"])
def search():
    query = request.args.get("query", "").strip()
    max_results = int(request.args.get("limit", "15"))
    
    if not query:
        return jsonify({"error": "Missing query parameter 'query'"}), 400
    
    try:
        results = sp.search(query, type="track", limit=max_results)
        tracks = results["tracks"]["items"]
        
        formatted = []
        for track in tracks:
            formatted.append({
                "title": track["name"],
                "artist": track["artists"][0]["name"] if track["artists"] else "",
                "duration_sec": track["duration_ms"] // 1000 if track.get("duration_ms") else None,
                "thumbnail_url": track["album"]["images"][0]["url"] if track["album"]["images"] else "",
                "url": track["external_urls"]["spotify"]
            })
        
        return jsonify({"data": {"tracks": formatted}}), 200
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

### Node.js (Express)

```javascript
const express = require("express");
const SpotifyWebApi = require("spotify-web-api-node");

const app = express();

const spotifyApi = new SpotifyWebApi({
  clientId: process.env.SPOTIFY_CLIENT_ID,
  clientSecret: process.env.SPOTIFY_CLIENT_SECRET,
});

spotifyApi.clientCredentialsFlow()
  .then((data) => {
    spotifyApi.setAccessToken(data.body["access_token"]);
  })
  .catch((err) => {
    console.error("Spotify auth failed:", err);
  });

app.get("/api/v1/search", async (req, res) => {
  const { query, limit } = req.query;

  if (!query) {
    return res.status(400).json({ error: "Missing query parameter 'query'" });
  }

  try {
    const results = await spotifyApi.searchTracks(query, { limit: parseInt(limit) || 15 });
    const formatted = results.body.tracks.items.map((track) => ({
      title: track.name,
      artist: track.artists[0]?.name || "",
      duration_sec: Math.floor(track.duration_ms / 1000),
      thumbnail_url: track.album.images[0]?.url || "",
      url: track.external_urls.spotify,
    }));

    res.json({ data: { tracks: formatted } });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.listen(5000, () => {
  console.log("Proxy server running on http://localhost:5000");
});
```

## Deployment Checklist

- [ ] Server code deployed and running
- [ ] Spotify credentials (Client ID/Secret) set in environment variables
- [ ] `/api/v1/search` and `/api/v1/resolve` endpoints respond correctly
- [ ] CORS headers enabled if running on different domain (add `Access-Control-Allow-Origin: *`)
- [ ] HTTPS configured for production (recommended)
- [ ] Rate limiting implemented to prevent abuse
- [ ] Logging enabled for debugging
- [ ] BananaFlow client configured with correct proxy server URL
- [ ] Test end-to-end: Search for a track in BananaFlow Spotify mode

## Testing

### Local Test

```bash
curl "http://localhost:5000/api/v1/search?query=example+artist+song&limit=5"
```

Expected response:
```json
{
  "results": [
    {
      "title": "Example Song One",
      "artist": "Example Artist",
      "duration_sec": 229,
      ...
    }
  ]
}
```

### From BananaFlow Client

1. Open Settings → Spotify Proxy Server
2. Enter: `http://localhost:5000`
3. Go to Search panel
4. Select "Spotify" or "Both" from platform dropdown
5. Search for a query
6. Results should appear within 2–5 seconds

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Cannot reach proxy server"                    | Check server is running, firewall allows connection |
| "Proxy returned HTTP 401"                     | Spotify credentials invalid/expired, rotate in env |
| "Proxy returned HTTP 500"                     | Check server logs, verify Spotify API connectivity |
| Results show but download fails               | Normal—YouTube search used for download, not Spotify |
| Search is very slow                           | May need to optimize Spotify API calls or add caching |

## CORS (Cross-Origin)

If your proxy server runs on a different domain (e.g., `https://spotify.example.com`) from where BananaFlow is hosted, you may need CORS headers:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, OPTIONS
Access-Control-Allow-Headers: Content-Type, User-Agent, X-App-Token
```

## Rate Limiting

Spotify API has rate limits. Implement caching or rate limiting to prevent hitting quotas:

- Cache results for 1 hour
- Limit to 100 searches per minute per IP
- Degrade gracefully if API is rate-limited

## Security Notes

- **Do not expose Client Secret**: It should only exist on your server, never in the client
- **Do not commit the app token**: Configure it in Settings or with `BANANAFLOW_SPOTIFY_APP_API_KEY`
- **Use HTTPS in production**: Protect API calls from interception
- **Validate input**: Sanitize query parameters to prevent injection attacks
- **Rate limit by IP**: Prevent abuse of your proxy server
- **Log safely**: Track errors and unusual usage patterns without credentials, cookies, or `X-App-Token`

---

**For questions or issues with this API, please refer to the main BananaFlow documentation or open an issue on GitHub.**
