from __future__ import annotations

import threading

from core.downloader import DownloadProgress, DownloadRequest, DownloadStatus
from core.download_orchestrator import DownloadOrchestrator
from core.match_errors import is_media_unavailable_error


class Callbacks:
    def __init__(self) -> None:
        self.errors = []
        self.finished = []

    def on_track_error(self, key, error):
        self.errors.append((key, error))

    def on_track_finished(self, key, path):
        self.finished.append((key, path))

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class Engine:
    def __init__(self, first_error: str) -> None:
        self._cancel_event = threading.Event()
        self.first_error = first_error
        self.urls = []

    def cancel_all(self):
        self._cancel_event.set()

    def download(self, req):
        self.urls.append(req.url)
        if len(self.urls) == 1 and self.first_error is not None:
            req.on_error(DownloadProgress(
                status=DownloadStatus.ERROR,
                url=req.url,
                error_message=self.first_error,
            ))
            return
        req.on_finished(DownloadProgress(
            status=DownloadStatus.FINISHED,
            url=req.url,
            downloaded_bytes=100,
            total_bytes=100,
            output_path="",
        ))


def _request(tmp_path):
    return DownloadRequest(
        url="https://youtube.test/old-upload",
        output_dir=str(tmp_path),
        spotify_match_identity={
            "spotify_id": "track-1",
            "title": "Song",
            "artist": "Artist",
            "duration_sec": 200,
        },
    )


def test_only_media_unavailable_errors_allow_one_rematch(tmp_path, monkeypatch):
    engine = Engine("ERROR: This video is unavailable")
    callbacks = Callbacks()
    invalidated = []
    resolved = []
    monkeypatch.setattr(
        "core.scraper.invalidate_track_match",
        lambda identity, url: invalidated.append((identity, url)) or True,
    )
    monkeypatch.setattr(
        "core.scraper.resolve_track_to_youtube",
        lambda identity, **kwargs: resolved.append((identity, kwargs))
        or "https://youtube.test/new-upload",
    )

    result = DownloadOrchestrator(engine, callbacks, max_workers=1).run_batch(
        [("k", _request(tmp_path))]
    )

    assert result.completed == 1
    assert result.failed == 0
    assert engine.urls == [
        "https://youtube.test/old-upload",
        "https://youtube.test/new-upload",
    ]
    assert invalidated[0][1] == "https://youtube.test/old-upload"
    assert resolved[0][1]["exclude_urls"] == {"https://youtube.test/old-upload"}


def test_auth_failure_never_changes_recording_candidate(tmp_path, monkeypatch):
    engine = Engine("Sign in to confirm your account")
    callbacks = Callbacks()
    monkeypatch.setattr(
        "core.scraper.resolve_track_to_youtube",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("auth failures must not trigger rematching")
        ),
    )

    result = DownloadOrchestrator(engine, callbacks, max_workers=1).run_batch(
        [("k", _request(tmp_path))]
    )
    assert result.failed == 1
    assert len(engine.urls) == 1


def test_unavailable_classifier_excludes_auth_rate_bot_and_geo():
    assert is_media_unavailable_error("private video")
    assert is_media_unavailable_error("This video is unavailable")
    for message in (
        "Sign in to continue",
        "HTTP 429 Too Many Requests",
        "Sign in to confirm you're not a bot",
        "not available in your country",
        "This video is unavailable in your country",
    ):
        assert not is_media_unavailable_error(message)


def test_private_video_with_generic_signin_advice_is_still_stale_media():
    assert is_media_unavailable_error(
        "Private video. Sign in if you've been granted access to this video"
    )


def test_empty_lazy_match_is_repaired_before_engine_submission(tmp_path):
    engine = Engine(None)
    callbacks = Callbacks()
    req = _request(tmp_path)
    req.url = "ytsearch1:Artist Song"
    req.url_resolver = lambda _event: ""

    result = DownloadOrchestrator(engine, callbacks, max_workers=1).run_batch(
        [("k", req)]
    )
    assert result.failed == 0
    assert result.completed == 1
    assert engine.urls == ["ytsearch1:Artist Song audio"]
    assert callbacks.errors == []
