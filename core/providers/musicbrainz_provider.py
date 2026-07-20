"""Read-only MusicBrainz provider with cooperative cancellation and throttling."""
from __future__ import annotations

from dataclasses import replace
from email.utils import parsedate_to_datetime
import threading
import time
from typing import Callable
from urllib.parse import quote

import httpx

from core.metadata_lookup import (
    ArtworkReference, CancellationToken, LookupMode, LookupRequest, LookupResult,
    LookupState, MetadataCandidate, ProviderAttribution, ProviderError,
    ProviderErrorKind, ReleaseDetailRequest, ReleaseDetailResult, ReleaseTrack,
)
from core.update_checker import CURRENT_VERSION


MB_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = f"BananaFlowDownloader/{CURRENT_VERSION} (https://github.com/BananaFlow-Media/BananaFlow)"
ATTRIBUTION = ProviderAttribution("musicbrainz", "meta_online_provider_musicbrainz", "MusicBrainz", "https://musicbrainz.org/")


class _RateLimiter:
    def __init__(self, interval_s: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.interval_s = interval_s; self.clock = clock; self._lock = threading.Lock(); self._last = 0.0

    def wait(self, cancellation: CancellationToken) -> bool:
        with self._lock:
            delay = max(0.0, self.interval_s - (self.clock() - self._last))
            if delay and cancellation.wait(delay):
                return False
            self._last = self.clock()
            return not cancellation.cancelled


_LIMITER = _RateLimiter()


def _credit(value) -> str:
    if isinstance(value, str): return value.strip()
    parts = []
    for entry in value or ():
        if not isinstance(entry, dict): continue
        artist = entry.get("artist") if isinstance(entry.get("artist"), dict) else {}
        name = entry.get("name") or artist.get("name") or ""
        if name: parts.append(str(name) + str(entry.get("joinphrase") or ""))
    return "".join(parts).strip()


def _int(value):
    try: return int(value)
    except (TypeError, ValueError): return None


class MusicBrainzProvider:
    provider_id = "musicbrainz"
    display_name_key = "meta_online_provider_musicbrainz"
    attribution = ATTRIBUTION
    supported_modes = frozenset({LookupMode.TRACK, LookupMode.ALBUM})

    def __init__(self, *, client=None, limiter=None, timeout_s: float = 10.0) -> None:
        self._client = client
        self._limiter = limiter or _LIMITER
        self.timeout = httpx.Timeout(timeout_s, connect=min(5.0, timeout_s))
        self._cache: dict[tuple, tuple[MetadataCandidate, ...]] = {}
        self._detail_cache: dict[tuple[str, str, str], MetadataCandidate] = {}

    @property
    def headers(self):
        return {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def lookup(self, request: LookupRequest, cancellation: CancellationToken) -> LookupResult:
        if request.provider_id != self.provider_id or request.mode not in self.supported_modes:
            return self._error(request, ProviderErrorKind.UNKNOWN, "meta_online_provider_error")
        if cancellation.cancelled:
            return self._cancelled(request)
        key = (request.mode.value, request.title, request.artist, request.album)
        if key in self._cache:
            cached = self._cache[key]
            return LookupResult(request, LookupState.READY if cached else LookupState.NO_RESULTS,
                                cached, from_cache=True)
        if not self._limiter.wait(cancellation):
            return self._cancelled(request)
        endpoint, query, inc = self._query(request)
        # Identity-free searches are never a valid MusicBrainz request.
        if not query:
            return LookupResult(request, LookupState.NO_RESULTS)
        try:
            client = self._client or httpx
            response = client.get(f"{MB_BASE}/{endpoint}", params={"query": query, "fmt": "json", "limit": 25, "inc": inc},
                                  headers=self.headers, timeout=self.timeout)
        except httpx.TimeoutException:
            return self._error(request, ProviderErrorKind.TIMEOUT, "meta_online_timeout", retryable=True)
        except httpx.TransportError:
            return self._error(request, ProviderErrorKind.OFFLINE, "meta_online_offline", retryable=True)
        except Exception:
            return self._error(request, ProviderErrorKind.UNKNOWN, "meta_online_provider_error", retryable=True)
        if cancellation.cancelled: return self._cancelled(request)
        if response.status_code == 429:
            retry = self._retry_after(response.headers.get("Retry-After"))
            return self._error(request, ProviderErrorKind.RATE_LIMITED, "meta_online_rate_limited", True, retry)
        if response.status_code == 503:
            return self._error(request, ProviderErrorKind.UNAVAILABLE, "meta_online_provider_unavailable", True)
        try:
            response.raise_for_status(); payload = response.json()
        except (ValueError, TypeError):
            return self._error(request, ProviderErrorKind.MALFORMED_RESPONSE, "meta_online_provider_error")
        except httpx.HTTPError:
            return self._error(request, ProviderErrorKind.UNKNOWN, "meta_online_provider_error", response.status_code >= 500)
        if not isinstance(payload, dict):
            return self._error(request, ProviderErrorKind.MALFORMED_RESPONSE, "meta_online_provider_error")
        try:
            candidates = self._normalize_releases(payload.get("releases") or ()) if request.mode is LookupMode.ALBUM else self._normalize_recordings(payload.get("recordings") or ())
        except (TypeError, ValueError, KeyError):
            return self._error(request, ProviderErrorKind.MALFORMED_RESPONSE, "meta_online_provider_error")
        state = LookupState.READY if candidates else LookupState.NO_RESULTS
        self._cache[key] = candidates
        return LookupResult(request, state, candidates)

    def lookup_release_detail(self, request: ReleaseDetailRequest,
                              cancellation: CancellationToken) -> ReleaseDetailResult:
        """Expand exactly one selected release.  Search responses are summaries."""
        if not request.release_id:
            return ReleaseDetailResult(request, LookupState.NO_RESULTS)
        if cancellation.cancelled:
            return ReleaseDetailResult(request, LookupState.CANCELLED,
                error=ProviderError(ProviderErrorKind.CANCELLED, "meta_online_cancelled"))
        cache_key = (self.provider_id, request.candidate_id, request.release_id)
        cached = self._detail_cache.get(cache_key)
        if cached is not None:
            if cancellation.cancelled:
                return ReleaseDetailResult(request, LookupState.CANCELLED,
                    error=ProviderError(ProviderErrorKind.CANCELLED, "meta_online_cancelled"))
            return ReleaseDetailResult(request, LookupState.READY, cached, from_cache=True)
        if not self._limiter.wait(cancellation):
            return ReleaseDetailResult(request, LookupState.CANCELLED,
                error=ProviderError(ProviderErrorKind.CANCELLED, "meta_online_cancelled"))
        try:
            response = (self._client or httpx).get(
                f"{MB_BASE}/release/{quote(request.release_id, safe='')}",
                params={"fmt": "json", "inc": "recordings+isrcs+artist-credits+release-groups+labels+genres+media"},
                headers=self.headers, timeout=self.timeout)
        except httpx.TimeoutException:
            return self._detail_error(request, ProviderErrorKind.TIMEOUT, "meta_online_timeout", True)
        except httpx.TransportError:
            return self._detail_error(request, ProviderErrorKind.OFFLINE, "meta_online_offline", True)
        except Exception:
            return self._detail_error(request, ProviderErrorKind.UNKNOWN, "meta_online_provider_error", True)
        if cancellation.cancelled:
            return ReleaseDetailResult(request, LookupState.CANCELLED,
                error=ProviderError(ProviderErrorKind.CANCELLED, "meta_online_cancelled"))
        if response.status_code == 429:
            return self._detail_error(request, ProviderErrorKind.RATE_LIMITED, "meta_online_rate_limited", True,
                                      self._retry_after(response.headers.get("Retry-After")))
        if response.status_code == 503:
            return self._detail_error(request, ProviderErrorKind.UNAVAILABLE, "meta_online_provider_unavailable", True)
        try:
            response.raise_for_status(); payload = response.json()
            candidates = self._normalize_releases((payload,), include_tracks=True) if isinstance(payload, dict) else ()
        except (ValueError, TypeError, KeyError, httpx.HTTPError):
            return self._detail_error(request, ProviderErrorKind.MALFORMED_RESPONSE, "meta_online_provider_error")
        if not candidates:
            return ReleaseDetailResult(request, LookupState.NO_RESULTS)
        candidate = candidates[0]
        self._detail_cache[cache_key] = candidate
        return ReleaseDetailResult(request, LookupState.READY, candidate)

    def lookup_downloader(self, request: LookupRequest, cancellation: CancellationToken) -> LookupResult:
        """Bounded compatibility fallback, used only by the downloader writer."""
        if not self._clean(request.title):
            return LookupResult(request, LookupState.NO_RESULTS)
        variants = [(request.title, request.artist, request.album)]
        if request.album:
            variants.append((request.title, request.artist, ""))
        variants.append((request.title, request.artist, "", True))
        last = LookupResult(request, LookupState.NO_RESULTS)
        for variant in variants:
            title, artist, album, *soft = variant
            result = self._lookup_track_query(request, title, artist, album, bool(soft), cancellation)
            if result is None:
                return self._cancelled(request)
            last = result
            if result.state is not LookupState.NO_RESULTS:
                return result
        return last

    def _lookup_track_query(self, request, title, artist, album, soft, cancellation):
        if cancellation.cancelled or not self._limiter.wait(cancellation):
            return None
        query = self._track_query(title, artist, album, soft)
        if not query:
            return LookupResult(request, LookupState.NO_RESULTS)
        try:
            response = (self._client or httpx).get(
                f"{MB_BASE}/recording", params={"query": query, "fmt": "json", "limit": 25,
                "inc": "releases+release-groups+isrcs+artist-credits+genres+labels+media"},
                headers=self.headers, timeout=self.timeout)
        except httpx.TimeoutException:
            return self._error(request, ProviderErrorKind.TIMEOUT, "meta_online_timeout", retryable=True)
        except httpx.TransportError:
            return self._error(request, ProviderErrorKind.OFFLINE, "meta_online_offline", retryable=True)
        except Exception:
            return self._error(request, ProviderErrorKind.UNKNOWN, "meta_online_provider_error", retryable=True)
        if cancellation.cancelled:
            return self._cancelled(request)
        if response.status_code == 429:
            return self._error(request, ProviderErrorKind.RATE_LIMITED, "meta_online_rate_limited", True,
                               self._retry_after(response.headers.get("Retry-After")))
        if response.status_code == 503:
            return self._error(request, ProviderErrorKind.UNAVAILABLE, "meta_online_provider_unavailable", True)
        try:
            response.raise_for_status(); payload = response.json()
            candidates = self._normalize_recordings(payload.get("recordings") or ()) if isinstance(payload, dict) else ()
        except (ValueError, TypeError, KeyError, httpx.HTTPError):
            return self._error(request, ProviderErrorKind.MALFORMED_RESPONSE, "meta_online_provider_error")
        return LookupResult(request, LookupState.READY if candidates else LookupState.NO_RESULTS, candidates)

    def _query(self, request: LookupRequest):
        def clean(text):
            from core.musicbrainz_enricher import _clean_search_term
            return clean_lucene(_clean_search_term(text))
        if request.mode is LookupMode.ALBUM:
            parts = [f'release:"{clean(request.album)}"'] if request.album else []
            if request.artist: parts.append(f'artist:"{clean(request.artist)}"')
            return "release", " AND ".join(parts), "artist-credits+release-groups+labels+genres"
        return "recording", self._track_query(request.title, request.artist, request.album), "releases+release-groups+isrcs+artist-credits+genres+labels+media"

    @staticmethod
    def _clean(value: str) -> str:
        from core.musicbrainz_enricher import _clean_search_term
        return clean_lucene(_clean_search_term(value))

    def _track_query(self, title: str, artist: str, album: str, soft: bool = False) -> str:
        title = self._clean(title); artist = self._clean(artist); album = self._clean(album)
        if not title:
            return ""
        if soft:
            parts = [f"recording:({title})"]
            if artist: parts.append(f"artist:({artist})")
        else:
            parts = [f'recording:"{title}"']
            if artist: parts.append(f'artist:"{artist}"')
            if album: parts.append(f'release:"{album}"')
        return " AND ".join(parts)

    def _normalize_recordings(self, recordings) -> tuple[MetadataCandidate, ...]:
        result = []
        for recording in recordings:
            if not isinstance(recording, dict) or not recording.get("id"): continue
            releases = recording.get("releases") or ({},)
            for release in releases:
                result.append(self._candidate(recording, release if isinstance(release, dict) else {}))
        return tuple(result)

    def _normalize_releases(self, releases, *, include_tracks: bool = False) -> tuple[MetadataCandidate, ...]:
        result = []
        for release in releases:
            if not isinstance(release, dict) or not release.get("id"): continue
            # Search results intentionally remain lightweight: MusicBrainz
            # does not promise complete media/tracks in /release search.
            tracks = self._release_tracks(release) if include_tracks else ()
            group = release.get("release-group") if isinstance(release.get("release-group"), dict) else {}
            publisher = self._publisher(release)
            result.append(MetadataCandidate(
                provider_id=self.provider_id, candidate_id=str(release["id"]), release_id=str(release["id"]),
                release_group_id=str(group.get("id") or ""), album=str(release.get("title") or ""),
                album_artist=_credit(release.get("artist-credit")), date=str(release.get("date") or ""),
                publisher=publisher, country=str(release.get("country") or ""),
                release_status=str(release.get("status") or ""), release_type=str(group.get("primary-type") or ""),
                artwork=ArtworkReference(str(release["id"]), True, f"https://coverartarchive.org/release/{release['id']}"),
                attribution=self.attribution, source_url=f"https://musicbrainz.org/release/{release['id']}", tracks=tracks,
            ))
        return tuple(result)

    def _candidate(self, recording, release) -> MetadataCandidate:
        release_id = str(release.get("id") or "")
        group = release.get("release-group") if isinstance(release.get("release-group"), dict) else {}
        media = release.get("media") or ()
        medium = media[0] if media and isinstance(media[0], dict) else {}
        genre_rows = recording.get("genres") or release.get("genres") or ()
        genre = max((row for row in genre_rows if isinstance(row, dict)), key=lambda row: row.get("count", 0), default={}).get("name", "")
        isrcs = recording.get("isrcs") or ()
        return MetadataCandidate(
            provider_id=self.provider_id, candidate_id=f"{recording['id']}:{release_id}", recording_id=str(recording["id"]),
            release_id=release_id, release_group_id=str(group.get("id") or ""), title=str(recording.get("title") or ""),
            artist=_credit(recording.get("artist-credit")), album=str(release.get("title") or ""),
            album_artist=_credit(release.get("artist-credit")), track_num=_int(recording.get("position")),
            track_total=_int(medium.get("track-count")), disc_num=_int(medium.get("position")), disc_total=len(media) or None,
            date=str(release.get("date") or ""), genre=str(genre).title(), isrc=str(isrcs[0]) if isrcs else "",
            publisher=self._publisher(release), country=str(release.get("country") or ""), duration_ms=_int(recording.get("length")),
            release_status=str(release.get("status") or ""), release_type=str(group.get("primary-type") or ""),
            artwork=ArtworkReference(release_id, bool(release_id), f"https://coverartarchive.org/release/{release_id}" if release_id else ""),
            attribution=self.attribution, source_url=f"https://musicbrainz.org/recording/{recording['id']}",
        )

    def _release_tracks(self, release) -> tuple[ReleaseTrack, ...]:
        result = []; media = release.get("media") or ()
        for disc_index, medium in enumerate(media, 1):
            if not isinstance(medium, dict): continue
            rows = medium.get("tracks") or ()
            total = _int(medium.get("track-count")) or len(rows) or None
            for track_index, row in enumerate(rows, 1):
                if not isinstance(row, dict): continue
                recording = row.get("recording") if isinstance(row.get("recording"), dict) else {}
                result.append(ReleaseTrack(
                    recording_id=str(recording.get("id") or ""), title=str(row.get("title") or recording.get("title") or ""),
                    artist=_credit(row.get("artist-credit") or recording.get("artist-credit")),
                    track_num=_int(row.get("number")) or _int(row.get("position")) or track_index, track_total=total,
                    disc_num=_int(medium.get("position")) or disc_index, disc_total=len(media) or None,
                    duration_ms=_int(row.get("length") or recording.get("length")),
                ))
        return tuple(result)

    @staticmethod
    def _publisher(release) -> str:
        for info in release.get("label-info") or ():
            if isinstance(info, dict) and isinstance(info.get("label"), dict) and info["label"].get("name"):
                return str(info["label"]["name"])
        return ""

    @staticmethod
    def _retry_after(value) -> float | None:
        if not value: return None
        try: return max(0.0, min(3600.0, float(value)))
        except ValueError:
            try: return max(0.0, min(3600.0, parsedate_to_datetime(value).timestamp() - time.time()))
            except Exception: return None

    @staticmethod
    def _cancelled(request):
        return LookupResult(request, LookupState.CANCELLED, error=ProviderError(ProviderErrorKind.CANCELLED, "meta_online_cancelled"))

    @staticmethod
    def _error(request, kind, key, retryable=False, retry_after=None):
        state = {ProviderErrorKind.OFFLINE: LookupState.OFFLINE, ProviderErrorKind.RATE_LIMITED: LookupState.RATE_LIMITED,
                 ProviderErrorKind.CANCELLED: LookupState.CANCELLED}.get(kind, LookupState.ERROR)
        return LookupResult(request, state, error=ProviderError(kind, key, retryable, retry_after))

    @staticmethod
    def _detail_error(request, kind, key, retryable=False, retry_after=None):
        state = {ProviderErrorKind.OFFLINE: LookupState.OFFLINE, ProviderErrorKind.RATE_LIMITED: LookupState.RATE_LIMITED,
                 ProviderErrorKind.CANCELLED: LookupState.CANCELLED}.get(kind, LookupState.ERROR)
        return ReleaseDetailResult(request, state, error=ProviderError(kind, key, retryable, retry_after))


def clean_lucene(value: str) -> str:
    return value.replace('"', " ").replace("\\", " ").strip()
