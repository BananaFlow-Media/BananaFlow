from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from core.metadata_lookup import (
    ArtworkCandidate, CancellationToken, LocalTrackSnapshot, LookupMode,
    LookupRequest, LookupResult, LookupState, ProviderErrorKind, ReleaseDetailRequest,
)
from core.providers.cover_art_archive_provider import CoverArtArchiveProvider
from core.providers.musicbrainz_provider import MusicBrainzProvider, USER_AGENT


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class Response:
    def __init__(self, status=200, payload=None, *, headers=None, content=b""):
        self.status_code = status; self._payload = payload; self.headers = headers or {}; self.content = content
        self.request = httpx.Request("GET", "https://example.test")

    def json(self):
        if isinstance(self._payload, Exception): raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400: raise httpx.HTTPStatusError("failed", request=self.request, response=self)


class Client:
    def __init__(self, response=None, error=None): self.response=response; self.error=error; self.calls=[]
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error: raise self.error
        return self.response


class NoWait:
    def __init__(self): self.calls=0
    def wait(self, token): self.calls += 1; return not token.cancelled


def request(mode=LookupMode.TRACK):
    local = LocalTrackSnapshot(4, title="Song", artist="Artist", album="Album", track_num=1, duration_ms=180_000)
    return LookupRequest("req", 3, 7, (4,), "musicbrainz", mode, "Song", "Artist", "Album", (local,))


RECORDING_PAYLOAD = {"recordings": [{
    "id": "rec-1", "title": "Song", "length": 181000,
    "artist-credit": [{"name": "Artist"}], "isrcs": ["USABC1234567"],
    "genres": [{"name": "rock", "count": 8}],
    "releases": [{"id": "rel-1", "title": "Album", "date": "2024-02-03", "country": "US",
                  "status": "Official", "artist-credit": [{"name": "Artist"}],
                  "release-group": {"id": "rg-1", "primary-type": "Album"},
                  "label-info": [{"label": {"name": "Label"}}],
                  "media": [{"position": 1, "track-count": 10}]}],
}]}


def test_musicbrainz_request_user_agent_timeout_and_normalization():
    client = Client(Response(payload=RECORDING_PAYLOAD)); limiter = NoWait()
    result = MusicBrainzProvider(client=client, limiter=limiter).lookup(request(), CancellationToken())
    assert result.state is LookupState.READY and len(result.candidates) == 1
    candidate = result.candidates[0]
    assert (candidate.recording_id, candidate.release_id, candidate.release_group_id) == ("rec-1", "rel-1", "rg-1")
    assert (candidate.genre, candidate.isrc, candidate.publisher, candidate.country) == ("Rock", "USABC1234567", "Label", "US")
    url, kwargs = client.calls[0]
    assert url.endswith("/recording") and kwargs["headers"]["User-Agent"] == USER_AGENT
    assert kwargs["params"]["query"] == 'recording:"Song" AND artist:"Artist" AND release:"Album"'
    assert isinstance(kwargs["timeout"], httpx.Timeout) and limiter.calls == 1


def test_musicbrainz_session_cache_avoids_second_request():
    client = Client(Response(payload=RECORDING_PAYLOAD)); provider = MusicBrainzProvider(client=client, limiter=NoWait())
    assert not provider.lookup(request(), CancellationToken()).from_cache
    assert provider.lookup(request(), CancellationToken()).from_cache
    assert len(client.calls) == 1


@pytest.mark.parametrize("status,headers,state,kind", [
    (429, {"Retry-After": "12"}, LookupState.RATE_LIMITED, ProviderErrorKind.RATE_LIMITED),
    (503, {}, LookupState.ERROR, ProviderErrorKind.UNAVAILABLE),
])
def test_musicbrainz_structured_http_failures(status, headers, state, kind):
    result = MusicBrainzProvider(client=Client(Response(status, {}, headers=headers)), limiter=NoWait()).lookup(request(), CancellationToken())
    assert result.state is state and result.error.kind is kind and result.error.retryable
    if status == 429: assert result.error.retry_after_s == 12


@pytest.mark.parametrize("error,kind,state", [
    (httpx.ConnectError("offline"), ProviderErrorKind.OFFLINE, LookupState.OFFLINE),
    (httpx.ReadTimeout("slow"), ProviderErrorKind.TIMEOUT, LookupState.ERROR),
])
def test_musicbrainz_network_and_timeout_are_structured(error, kind, state):
    result = MusicBrainzProvider(client=Client(error=error), limiter=NoWait()).lookup(request(), CancellationToken())
    assert result.error.kind is kind and result.state is state


def test_musicbrainz_malformed_json_and_cancellation_do_not_mutate_or_call():
    malformed = MusicBrainzProvider(client=Client(Response(payload=ValueError("json"))), limiter=NoWait()).lookup(request(), CancellationToken())
    assert malformed.error.kind is ProviderErrorKind.MALFORMED_RESPONSE
    token = CancellationToken(); token.cancel(); client = Client(Response(payload=RECORDING_PAYLOAD))
    cancelled = MusicBrainzProvider(client=client, limiter=NoWait()).lookup(request(), token)
    assert cancelled.state is LookupState.CANCELLED and not client.calls


def test_album_release_search_stays_lightweight_and_never_writes(monkeypatch):
    payload = {"releases": [{"id": "rel", "title": "Album", "artist-credit": [{"name": "Artist"}],
        "release-group": {"id": "rg", "primary-type": "Album"},
        "media": [{"position": 1, "track-count": 2, "tracks": [
            {"position": 1, "number": "1", "title": "First", "length": 100000, "recording": {"id": "r1"}},
            {"position": 2, "number": "2", "title": "Second", "length": 110000, "recording": {"id": "r2"}},
        ]}]}]}
    monkeypatch.setattr("core.metadata_processor.write_tags", lambda *a, **k: pytest.fail("lookup wrote tags"))
    result = MusicBrainzProvider(client=Client(Response(payload=payload)), limiter=NoWait()).lookup(request(LookupMode.ALBUM), CancellationToken())
    assert result.candidates[0].tracks == ()


def test_album_search_summary_expands_selected_mbid_and_maps_real_tracks(monkeypatch):
    search = Response(payload={"releases": [{
        "id": "rel", "title": "Album", "artist-credit": [{"name": "Album Artist"}],
        "release-group": {"id": "rg", "primary-type": "Album"}, "status": "Official",
        "date": "2024-02-03", "country": "US", "label-info": [{"label": {"name": "Label"}}],
    }]})
    detail = Response(payload={
        "id": "rel", "title": "Album", "artist-credit": [{"name": "Album Artist"}],
        "release-group": {"id": "rg", "primary-type": "Album"}, "status": "Official",
        "date": "2024-02-03", "country": "US", "label-info": [{"label": {"name": "Label"}}],
        "media": [{"position": 1, "track-count": 2, "tracks": [
            {"position": 1, "number": "1", "title": "Song", "length": 180000,
             "artist-credit": [{"name": "Album Artist"}], "recording": {"id": "r1", "title": "Song"}},
            {"position": 2, "number": "2", "title": "Second", "length": 200000,
             "artist-credit": [{"name": "Album Artist"}], "recording": {"id": "r2", "title": "Second"}},
        ]}],
    })
    class QueueClient:
        def __init__(self): self.responses=[search, detail]; self.calls=[]
        def get(self, url, **kwargs): self.calls.append((url, kwargs)); return self.responses.pop(0)
    client = QueueClient(); provider = MusicBrainzProvider(client=client, limiter=NoWait())
    lookup_request = request(LookupMode.ALBUM)
    summary = provider.lookup(lookup_request, CancellationToken())
    assert summary.candidates[0].tracks == ()
    detail_request = ReleaseDetailRequest("detail", lookup_request, "rel", "rel")
    expanded = provider.lookup_release_detail(detail_request, CancellationToken())
    assert expanded.state is LookupState.READY
    assert [track.recording_id for track in expanded.candidate.tracks] == ["r1", "r2"]
    assert client.calls[1][0].endswith("/release/rel")
    assert "recordings" in client.calls[1][1]["params"]["inc"]
    from core.metadata_match_service import MetadataMatchService
    scored = MetadataMatchService().score_candidate(lookup_request, expanded.candidate)
    assert any(e.component == "artist" and e.similarity > 0 for e in scored.evidence)
    mapping = MetadataMatchService().map_album(lookup_request.local_tracks, expanded.candidate.tracks)
    assert mapping[0].state.value == "matched"


def test_cover_art_archive_metadata_thumbnail_validation_and_attribution():
    metadata = Response(payload={"release": "https://musicbrainz.org/release/rel", "images": [{
        "id": 9, "image": "https://img/full.jpg", "front": True, "back": False,
        "types": ["Front"], "mime-type": "image/png", "thumbnails": {"500": "https://img/500.png"},
    }]})
    class CaaClient:
        def __init__(self): self.calls=[]
        def get(self, url, **kwargs):
            self.calls.append(url)
            return metadata if "/release/" in url else Response(headers={"Content-Type": "image/png"}, content=PNG)
    client = CaaClient(); provider = CoverArtArchiveProvider(client=client); token = CancellationToken()
    candidates = provider.list_artwork("rel", token)
    assert candidates[0].front and candidates[0].thumbnail_url.endswith("500.png")
    assert candidates[0].attribution.text == "Cover Art Archive"
    assert provider.download_preview(candidates[0], token) == PNG
    assert provider.download_full(candidates[0], token) == PNG
    assert client.calls == ["https://coverartarchive.org/release/rel", "https://img/500.png", "https://img/full.jpg"]


def test_downloader_fallback_is_bounded_and_never_wildcard():
    empty = Response(payload={"recordings": []})
    found = Response(payload=RECORDING_PAYLOAD)
    class QueueClient:
        def __init__(self): self.responses=[empty, empty, found]; self.calls=[]
        def get(self, url, **kwargs): self.calls.append(kwargs["params"]["query"]); return self.responses.pop(0)
    client = QueueClient(); provider = MusicBrainzProvider(client=client, limiter=NoWait())
    result = provider.lookup_downloader(request(), CancellationToken())
    assert result.state is LookupState.READY
    assert client.calls == [
        'recording:"Song" AND artist:"Artist" AND release:"Album"',
        'recording:"Song" AND artist:"Artist"',
        'recording:(Song) AND artist:(Artist)',
    ]
    empty_title = LookupRequest("empty", 0, 0, (4,), "musicbrainz", LookupMode.TRACK,
        local_tracks=(LocalTrackSnapshot(4),))
    assert provider.lookup_downloader(empty_title, CancellationToken()).state is LookupState.NO_RESULTS
    assert all(query != "*" for query in client.calls)


def test_cover_art_archive_rejects_mime_size_and_honours_cancellation():
    candidate = ArtworkCandidate("cover_art_archive", "rel", "1", (), True, False, "", "https://img", "https://full", "", CoverArtArchiveProvider.attribution)
    token = CancellationToken(); token.cancel(); client = Client(Response(headers={"Content-Type": "image/png"}, content=PNG))
    assert CoverArtArchiveProvider(client=client).download_preview(candidate, token) == b"" and not client.calls
    with pytest.raises(ValueError, match="mime"):
        CoverArtArchiveProvider(client=Client(Response(headers={"Content-Type": "text/html"}, content=b"x"))).download_preview(candidate, CancellationToken())


def test_downloader_compatibility_adapter_reuses_provider_mapping(monkeypatch, tmp_path):
    from core.metadata_lookup import MetadataCandidate, ProviderAttribution
    from core.musicbrainz_enricher import enrich_file
    attr = ProviderAttribution("musicbrainz", "key", "MusicBrainz", "https://musicbrainz.org")
    candidate = MetadataCandidate("musicbrainz", "c", recording_id="r", release_id="rel",
                                  title="Song", artist="Artist", album="Album", duration_ms=180000,
                                  genre="Rock", date="2024-01-02", isrc="CODE", publisher="Label",
                                  country="US", attribution=attr)
    monkeypatch.setattr(MusicBrainzProvider, "lookup_downloader", lambda self, req, token: LookupResult(req, LookupState.READY, (candidate,)))
    written = []
    monkeypatch.setattr("core.musicbrainz_enricher._write_enriched_tags", lambda path, tags: written.append((path, tags)) or True)
    assert enrich_file(str(tmp_path / "song.mp3"), "Song", "Artist", "Album", 180)
    assert written[0][1] == {"ISRC": "CODE", "GENRE": "Rock", "YEAR": "2024", "LABEL": "Label", "RELEASECOUNTRY": "US"}


def test_downloader_pipeline_is_opt_in_and_passes_album_duration(monkeypatch, tmp_path):
    from core.downloader import DownloadEngine, DownloadRequest
    calls=[]; path = tmp_path / "song.mp3"; path.write_bytes(b"audio")
    monkeypatch.setattr("core.downloader.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("core.musicbrainz_enricher.enrich_file", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    engine = DownloadEngine()
    disabled = DownloadRequest("url", str(tmp_path), forced_title="Song", forced_artist="Artist",
                               forced_album="Album", forced_duration=180, musicbrainz=False)
    assert engine._run_final_pipeline(disabled, str(path)) == [] and calls == []
    enabled = DownloadRequest("url", str(tmp_path), forced_title="Song", forced_artist="Artist",
                              forced_album="Album", forced_duration=180, musicbrainz=True)
    assert engine._run_final_pipeline(enabled, str(path)) == []
    assert calls[0][1] == {"title": "Song", "artist": "Artist", "album": "Album", "duration_s": 180}
