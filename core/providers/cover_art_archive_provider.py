"""Explicit Cover Art Archive metadata and preview provider."""
from __future__ import annotations

import httpx

from core.artwork import MAX_ENCODED_BYTES, validate_artwork_bytes
from core.metadata_lookup import ArtworkCandidate, CancellationToken, ProviderAttribution
from core.providers.musicbrainz_provider import USER_AGENT


CAA_BASE = "https://coverartarchive.org"
ATTRIBUTION = ProviderAttribution("cover_art_archive", "meta_online_provider_caa", "Cover Art Archive", "https://coverartarchive.org/")


class CoverArtArchiveProvider:
    provider_id = "cover_art_archive"
    display_name_key = "meta_online_provider_caa"
    attribution = ATTRIBUTION

    def __init__(self, *, client=None, timeout_s: float = 10.0) -> None:
        self._client = client
        self.timeout = httpx.Timeout(timeout_s, connect=min(5.0, timeout_s))

    @property
    def headers(self):
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def list_artwork(self, release_id: str, cancellation: CancellationToken) -> tuple[ArtworkCandidate, ...]:
        if cancellation.cancelled: return ()
        response = (self._client or httpx).get(f"{CAA_BASE}/release/{release_id}", headers=self.headers, timeout=self.timeout)
        response.raise_for_status(); payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("images", []), list):
            raise ValueError("malformed artwork response")
        result = []
        for row in payload.get("images", []):
            if not isinstance(row, dict) or not row.get("image"): continue
            thumbs = row.get("thumbnails") if isinstance(row.get("thumbnails"), dict) else {}
            result.append(ArtworkCandidate(
                self.provider_id, release_id, str(row.get("id") or row.get("image")), tuple(str(v) for v in row.get("types") or ()),
                bool(row.get("front")), bool(row.get("back")), str(row.get("mime-type") or ""),
                str(thumbs.get("500") or thumbs.get("250") or thumbs.get("small") or row["image"]), str(row["image"]),
                str(payload.get("release") or f"{CAA_BASE}/release/{release_id}"), self.attribution,
            ))
        return tuple(sorted(result, key=lambda item: (not item.front, item.back, item.image_id)))

    def download_preview(self, candidate: ArtworkCandidate, cancellation: CancellationToken) -> bytes:
        return self._download(candidate.thumbnail_url, cancellation)

    def download_full(self, candidate: ArtworkCandidate, cancellation: CancellationToken) -> bytes:
        """Fetch the original only for explicit final artwork acceptance."""
        return self._download(candidate.image_url or candidate.thumbnail_url, cancellation)

    def _download(self, url: str, cancellation: CancellationToken) -> bytes:
        if cancellation.cancelled: return b""
        response = (self._client or httpx).get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type not in {"image/jpeg", "image/png"}:
            raise ValueError("unsupported artwork mime")
        data = bytes(response.content)
        if len(data) > MAX_ENCODED_BYTES:
            raise ValueError("artwork too large")
        if cancellation.cancelled: return b""
        validate_artwork_bytes(data)
        return data
