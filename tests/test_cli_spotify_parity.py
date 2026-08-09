"""
tests/test_cli_spotify_parity.py
================================
Issue #59 — the CLI must build the SAME Spotify request as the desktop app.

`PlaylistParser` emits Spotify album/playlist/artist tracks as metadata-only
items: `match_status="pending"` and a `ytsearch1:` placeholder URL. Stage 2
resolves each one to a real YouTube/YTM URL, lazily, through
`DownloadRequest.url_resolver`.

`cli.py` used to skip stage 2 entirely — it built requests straight from
`track.url` and handed the placeholder to yt-dlp, which downloads the first
free-text search hit while bypassing the scorer, the album-aware search chain
and the persistent match cache. Both front-ends now go through
`core.spotify_request_builder`.

These tests drive the REAL code on both sides — `DownloadController.start_batch`
for the GUI and `cli.main()` for the CLI — with the download worker and the
orchestrator faked out, and compare the requests they produce.

Offline: no network, no real yt-dlp, no real match resolution (the one
network entry point, `core.scraper.resolve_track_to_youtube`, is
monkeypatched and its calls are recorded).
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.playlist_parser import (  # noqa: E402
    ParseResult,
    SourcePlatform,
    TrackMeta,
    UrlKind,
)
from core.spotify_request_builder import (  # noqa: E402
    attach_spotify_matching,
    build_spotify_resolver,
    effective_match_status,
    is_downloadable,
    spotify_identity,
)

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


SPOTIFY_ALBUM_URL = "https://open.spotify.com/album/TESTALBUMID00001"
RESOLVED_URL = "https://music.youtube.com/watch?v=RESOLVED1234"

# The six fields that make up the matching contract.
IDENTITY_KEYS = {
    "spotify_id", "spotify_key_kind", "title", "album", "artist", "duration_sec",
}


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / fakes
# ──────────────────────────────────────────────────────────────────────────────

def _pending_track(index: int, title: str, sid: str) -> TrackMeta:
    """One stage-1 Spotify item, exactly as PlaylistParser emits it."""
    return TrackMeta(
        index=index,
        url=f"ytsearch1:Test Artist {title} audio",   # the placeholder
        title=title,
        artist="Test Artist",
        album="Test Album",
        duration_sec=201 + index,
        platform=SourcePlatform.SPOTIFY,
        source_kind=UrlKind.ALBUM.name,
        source_url=SPOTIFY_ALBUM_URL,
        spotify_id=sid,
        spotify_key_kind="spotify_id",
        match_status="pending",
    )


class _FakeCard:
    """A queue card carrying the same stage-1 state as the TrackMeta above."""

    def __init__(self, track: TrackMeta) -> None:
        self.title = track.title
        self.artist = track.artist
        self.album = track.album
        self.parent_artist = ""
        self.release_type = ""
        self.platform = track.platform.value
        self.category = ""
        self.total_tracks = 0
        self.album_index = track.index
        self.disc_number = 0
        self.queue_index = track.index
        self.track_url = track.url
        self.thumbnail_url = ""
        self.duration_sec = track.duration_sec
        self.source_kind = track.source_kind
        self.source_url = track.source_url
        self.spotify_id = track.spotify_id
        self.spotify_key_kind = track.spotify_key_kind
        self.match_status = track.match_status
        self.resolution_error = track.resolution_error
        self._status = "queued"
        self.invalidated_with: list[str] = []

    def set_status(self, status: str) -> None:
        self._status = status

    def get_status(self) -> str:
        return self._status

    def set_progress(self, fraction: float) -> None:
        pass

    def is_selected(self) -> bool:
        return True

    def mark_metadata_invalid(self, message: str) -> None:
        self.invalidated_with.append(message)


class _FakeSignal:
    def connect(self, *_a, **_k) -> None:
        pass


class _FakeDownloadWorker:
    """Captures the jobs start_batch built instead of running a QThread."""

    last_instance: "_FakeDownloadWorker | None" = None

    def __init__(self, jobs, engine, config, db=None, max_workers=3,
                 preexisting=None, batch_id=None, parent=None) -> None:
        self.jobs = jobs
        self.preexisting = preexisting or []
        self.started = False
        for name in (
            "track_progress", "track_speed", "track_status", "track_phase",
            "track_finished", "track_preexisting", "overall_progress", "metrics",
            "batch_snapshot", "job_count_changed", "job_error", "all_finished",
            "track_thumbnail",
        ):
            setattr(self, name, _FakeSignal())
        _FakeDownloadWorker.last_instance = self

    def start(self) -> None:
        self.started = True

    def isRunning(self) -> bool:
        return self.started


@pytest.fixture
def resolver_calls(monkeypatch):
    """Record every call into the shared matching entry point."""
    calls: list[dict] = []

    def _fake_resolve(td, cookies_file=None, cancel_check=None, **kwargs):
        calls.append(dict(td))
        td["_match_source"] = "live"
        return RESOLVED_URL

    monkeypatch.setattr("core.scraper.resolve_track_to_youtube", _fake_resolve)
    # Local-only cache peek used to seed resolve_source — keep it off disk.
    monkeypatch.setattr("core.scraper.track_match_source_hint", lambda td: "live")
    return calls


# ──────────────────────────────────────────────────────────────────────────────
# The shared builder itself
# ──────────────────────────────────────────────────────────────────────────────

class TestSpotifyIdentity:
    def test_same_identity_from_track_card_and_dict(self):
        """The three shapes the app carries a track in must agree."""
        track = _pending_track(1, "Song One", "sid1")
        card = _FakeCard(track)
        persisted = {
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "duration_sec": track.duration_sec,
            "spotify_id": track.spotify_id,
            "spotify_key_kind": track.spotify_key_kind,
            "match_status": track.match_status,
        }

        from_track = spotify_identity(track)
        assert from_track == spotify_identity(card)
        assert from_track == spotify_identity(persisted)
        assert set(from_track) == IDENTITY_KEYS
        assert from_track["spotify_id"] == "sid1"
        assert from_track["album"] == "Test Album"

    def test_missing_fields_fall_back_to_contract_defaults(self):
        identity = spotify_identity({})
        assert set(identity) == IDENTITY_KEYS
        assert identity["spotify_key_kind"] == "spotify_id"
        assert identity["duration_sec"] is None


class TestAdmissionRule:
    def test_pending_is_downloadable_without_a_url(self):
        track = _pending_track(1, "Song", "sid")
        track.url = ""
        assert is_downloadable(track) is True

    def test_metadata_invalid_is_rejected(self):
        track = _pending_track(1, "Song", "sid")
        track.match_status = "metadata_invalid"
        track.url = ""
        assert is_downloadable(track) is False

    def test_unresolved_spotify_retries_as_pending(self):
        track = _pending_track(1, "Song", "sid")
        track.match_status = "unresolved"
        assert effective_match_status(track) == "pending"
        assert is_downloadable(track) is True

    def test_matched_track_without_a_url_is_rejected(self):
        track = TrackMeta(title="Song", url="", platform=SourcePlatform.YOUTUBE)
        assert is_downloadable(track) is False

    def test_matched_youtube_track_with_a_url_passes(self):
        track = TrackMeta(
            title="Song",
            url="https://www.youtube.com/watch?v=abc",
            platform=SourcePlatform.YOUTUBE,
        )
        assert is_downloadable(track) is True


class TestAttachMatching:
    def test_pending_gets_resolver_and_identity(self, resolver_calls):
        from core.downloader import DownloadRequest, MediaType

        track = _pending_track(1, "Song", "sid")
        req = DownloadRequest(
            url=track.url, output_dir=tempfile.gettempdir(),
            media_type=MediaType.AUDIO, platform=SourcePlatform.SPOTIFY,
        )
        assert attach_spotify_matching(req, track, None) is True
        assert req.url_resolver is not None
        assert req.spotify_match_identity == spotify_identity(track)

        assert req.url_resolver(None) == RESOLVED_URL
        assert resolver_calls[0]["spotify_id"] == "sid"
        assert resolver_calls[0]["album"] == "Test Album"

    def test_identity_copy_is_not_mutated_by_the_resolver(self, resolver_calls):
        """The resolver stamps "_match_source" into its own dict, not the
        identity recorded on the request."""
        from core.downloader import DownloadRequest, MediaType

        track = _pending_track(1, "Song", "sid")
        req = DownloadRequest(
            url=track.url, output_dir=tempfile.gettempdir(),
            media_type=MediaType.AUDIO, platform=SourcePlatform.SPOTIFY,
        )
        attach_spotify_matching(req, track, None)
        req.url_resolver(None)
        assert set(req.spotify_match_identity) == IDENTITY_KEYS

    def test_matched_spotify_gets_identity_but_no_resolver(self, resolver_calls):
        from core.downloader import DownloadRequest, MediaType

        track = _pending_track(1, "Song", "sid")
        track.match_status = "matched"
        track.url = "https://www.youtube.com/watch?v=already"
        req = DownloadRequest(
            url=track.url, output_dir=tempfile.gettempdir(),
            media_type=MediaType.AUDIO, platform=SourcePlatform.SPOTIFY,
        )
        assert attach_spotify_matching(req, track, None) is False
        assert req.url_resolver is None
        assert req.spotify_match_identity == spotify_identity(track)

    def test_youtube_request_is_untouched(self, resolver_calls):
        from core.downloader import DownloadRequest, MediaType

        track = TrackMeta(
            title="Song", artist="A",
            url="https://www.youtube.com/watch?v=abc",
            platform=SourcePlatform.YOUTUBE,
        )
        req = DownloadRequest(
            url=track.url, output_dir=tempfile.gettempdir(),
            media_type=MediaType.AUDIO, platform=SourcePlatform.YOUTUBE,
        )
        assert attach_spotify_matching(req, track, None) is False
        assert req.url_resolver is None
        assert req.spotify_match_identity is None

    def test_resolver_cancel_event_reaches_the_matcher(self, monkeypatch):
        import threading

        seen: dict = {}

        def _fake_resolve(td, cookies_file=None, cancel_check=None, **kwargs):
            seen["cancelled"] = cancel_check()
            return RESOLVED_URL

        monkeypatch.setattr("core.scraper.resolve_track_to_youtube", _fake_resolve)
        monkeypatch.setattr("core.scraper.track_match_source_hint", lambda td: "live")

        ev = threading.Event()
        ev.set()
        resolver = build_spotify_resolver(spotify_identity(_pending_track(1, "S", "sid")), None)
        resolver(ev)
        assert seen["cancelled"] is True


# ──────────────────────────────────────────────────────────────────────────────
# CLI ⟷ GUI parity, driving both real code paths
# ──────────────────────────────────────────────────────────────────────────────

def _parse_result(tracks: list[TrackMeta]) -> ParseResult:
    return ParseResult(
        url=SPOTIFY_ALBUM_URL,
        kind=UrlKind.ALBUM,
        platform=SourcePlatform.SPOTIFY,
        playlist_title="Test Album",
        total_count=len(tracks),
        tracks=tracks,
    )


def _run_cli(tracks, tmp_path, monkeypatch, extra_args=()) -> list:
    """Drive cli.main() end to end; return the jobs it handed the orchestrator."""
    import cli as cli_module
    from core.download_orchestrator import BatchResult

    monkeypatch.setattr(
        "core.runtime_components.activate_bundled_components", lambda: None,
    )
    monkeypatch.setattr(
        "core.playlist_parser.PlaylistParser.parse",
        lambda self, url, **kwargs: _parse_result(tracks),
    )

    class _FakeHistoryDB:
        def close(self) -> None:
            pass

    monkeypatch.setattr("core.history_db.HistoryDB", _FakeHistoryDB)

    captured: list = []

    class _FakeOrchestrator:
        def __init__(self, **kwargs) -> None:
            pass

        def run_batch(self, jobs):
            captured.extend(jobs)
            return BatchResult(
                total=len(jobs), completed=len(jobs), failed=0, cancelled=False,
            )

    monkeypatch.setattr(
        "core.download_orchestrator.DownloadOrchestrator", _FakeOrchestrator,
    )

    argv = ["cli.py", SPOTIFY_ALBUM_URL, "-o", str(tmp_path), "--quiet", *extra_args]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli_module.main() == 0
    return captured


def _run_gui(tracks, tmp_path, monkeypatch, app) -> list:
    """Drive DownloadController.start_batch; return the jobs it built."""
    import ui.workers.download_worker as download_worker_module

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(download_worker_module, "DownloadWorker", _FakeDownloadWorker)
    monkeypatch.setattr("core.duplicate_checker.find_duplicate", lambda **kwargs: None)

    from config import AppConfig
    from core.downloader import DownloadEngine
    from ui.controllers.download_controller import DownloadController

    ctrl = DownloadController(AppConfig(), DownloadEngine())
    # No duplicate dialog can interfere: find_duplicate is stubbed to None and
    # "overwrite" would not prompt anyway.
    ctrl._cfg.duplicate_action = "overwrite"
    opts = {
        "media_type": "audio",
        "quality_label": "audio_mp3_320",
        "audio_format": "mp3",
        "video_format": "mp4",
        "output_dir": str(tmp_path),
    }
    ctrl.start_batch(
        [_FakeCard(t) for t in tracks], opts, UrlKind.ALBUM, "Test Album",
    )
    worker = _FakeDownloadWorker.last_instance
    assert worker is not None
    return worker.jobs


@pytest.fixture(scope="module")
def app():
    if QApplication is None:  # pragma: no cover
        pytest.skip("PySide6 not available")
    return QApplication.instance() or QApplication([])


class TestCliSpotifyRequests:
    def test_pending_tracks_get_a_lazy_resolver(self, tmp_path, monkeypatch, resolver_calls):
        tracks = [_pending_track(i, f"Song {i}", f"sid{i}") for i in (1, 2, 3)]
        jobs = _run_cli(tracks, tmp_path, monkeypatch)

        assert len(jobs) == 3
        for _key, req in jobs:
            assert req.url_resolver is not None, "CLI built a request with no stage-2 resolver"
            assert set(req.spotify_match_identity) == IDENTITY_KEYS
            assert req.platform == SourcePlatform.SPOTIFY

    def test_final_url_is_resolved_not_a_placeholder(self, tmp_path, monkeypatch, resolver_calls):
        """The acceptance criterion: what reaches the engine is a real URL."""
        tracks = [_pending_track(1, "Song One", "sid1")]
        jobs = _run_cli(tracks, tmp_path, monkeypatch)
        _key, req = jobs[0]

        assert req.url.startswith("ytsearch1:")     # placeholder before stage 2
        resolved = req.url_resolver(None)
        assert resolved == RESOLVED_URL
        assert not resolved.startswith("ytsearch")

    def test_resolver_goes_through_the_shared_match_entry_point(
        self, tmp_path, monkeypatch, resolver_calls,
    ):
        """Match cache + scorer are shared with the GUI because both call the
        same resolve_track_to_youtube with the same identity."""
        tracks = [_pending_track(1, "Song One", "sid1")]
        jobs = _run_cli(tracks, tmp_path, monkeypatch)
        jobs[0][1].url_resolver(None)

        assert len(resolver_calls) == 1
        call = resolver_calls[0]
        assert call["spotify_id"] == "sid1"
        assert call["spotify_key_kind"] == "spotify_id"
        assert call["title"] == "Song One"
        assert call["album"] == "Test Album"
        assert call["duration_sec"] == 202

    def test_metadata_invalid_tracks_are_skipped(self, tmp_path, monkeypatch, resolver_calls):
        good = _pending_track(1, "Good", "sid1")
        bad = _pending_track(2, "Bad", "")
        bad.match_status = "metadata_invalid"
        bad.url = ""

        jobs = _run_cli([good, bad], tmp_path, monkeypatch)
        assert len(jobs) == 1
        assert jobs[0][1].spotify_match_identity["title"] == "Good"

    def test_all_tracks_invalid_exits_nonzero(self, tmp_path, monkeypatch, resolver_calls):
        import cli as cli_module
        bad = _pending_track(1, "Bad", "")
        bad.match_status = "metadata_invalid"
        bad.url = ""

        monkeypatch.setattr(
            "core.runtime_components.activate_bundled_components", lambda: None,
        )
        monkeypatch.setattr(
            "core.playlist_parser.PlaylistParser.parse",
            lambda self, url, **kwargs: _parse_result([bad]),
        )
        monkeypatch.setattr(
            sys, "argv",
            ["cli.py", SPOTIFY_ALBUM_URL, "-o", str(tmp_path), "--quiet"],
        )
        assert cli_module.main() == 1

    def test_non_spotify_tracks_keep_their_url_and_stay_unresolved(
        self, tmp_path, monkeypatch, resolver_calls,
    ):
        """Existing non-Spotify CLI behaviour is unchanged."""
        yt = TrackMeta(
            index=1,
            url="https://www.youtube.com/watch?v=abcdefghijk",
            title="YT Song",
            artist="YT Artist",
            album="Some Playlist",
            duration_sec=180,
            platform=SourcePlatform.YOUTUBE,
        )
        jobs = _run_cli([yt], tmp_path, monkeypatch)

        assert len(jobs) == 1
        _key, req = jobs[0]
        assert req.url == "https://www.youtube.com/watch?v=abcdefghijk"
        assert req.url_resolver is None
        assert req.spotify_match_identity is None
        assert req.platform == SourcePlatform.YOUTUBE
        # TrackMeta.album is the PLAYLIST title on YouTube — never forced.
        assert req.forced_album is None
        assert resolver_calls == []

    def test_list_mode_still_prints_every_track(self, tmp_path, monkeypatch, capsys):
        import cli as cli_module
        good = _pending_track(1, "Good", "sid1")
        bad = _pending_track(2, "Bad", "")
        bad.match_status = "metadata_invalid"
        bad.url = ""

        monkeypatch.setattr(
            "core.runtime_components.activate_bundled_components", lambda: None,
        )
        monkeypatch.setattr(
            "core.playlist_parser.PlaylistParser.parse",
            lambda self, url, **kwargs: _parse_result([good, bad]),
        )
        monkeypatch.setattr(
            sys, "argv", ["cli.py", SPOTIFY_ALBUM_URL, "--list", "--quiet"],
        )
        assert cli_module.main() == 0
        out = capsys.readouterr().out
        assert "Good" in out and "Bad" in out


class TestCliGuiParity:
    def test_identical_match_identity_and_resolution(
        self, tmp_path, monkeypatch, resolver_calls, app,
    ):
        """Same album through both front-ends → same matching contract."""
        tracks = [_pending_track(i, f"Song {i}", f"sid{i}") for i in (1, 2)]

        gui_jobs = _run_gui(tracks, tmp_path / "gui", monkeypatch, app)
        cli_jobs = _run_cli(tracks, tmp_path / "cli", monkeypatch)

        assert len(gui_jobs) == len(cli_jobs) == 2

        gui_by_title = {
            r.spotify_match_identity["title"]: r for _k, r in gui_jobs
        }
        cli_by_title = {
            r.spotify_match_identity["title"]: r for _k, r in cli_jobs
        }
        assert set(gui_by_title) == set(cli_by_title)

        for title, gui_req in gui_by_title.items():
            cli_req = cli_by_title[title]
            assert gui_req.spotify_match_identity == cli_req.spotify_match_identity
            assert (gui_req.url_resolver is None) == (cli_req.url_resolver is None)
            assert gui_req.url_resolver is not None
            assert gui_req.url_resolver(None) == cli_req.url_resolver(None) == RESOLVED_URL

    def test_both_report_the_same_resolve_source_hint(
        self, tmp_path, monkeypatch, resolver_calls, app,
    ):
        """The orchestrator sizes its resolver pool from this attribute."""
        tracks = [_pending_track(1, "Song 1", "sid1")]

        gui_jobs = _run_gui(tracks, tmp_path / "gui", monkeypatch, app)
        cli_jobs = _run_cli(tracks, tmp_path / "cli", monkeypatch)

        assert getattr(gui_jobs[0][1].url_resolver, "resolve_source") == \
               getattr(cli_jobs[0][1].url_resolver, "resolve_source")

    def test_both_send_the_matcher_the_same_track_dict(
        self, tmp_path, monkeypatch, resolver_calls, app,
    ):
        tracks = [_pending_track(1, "Song 1", "sid1")]

        gui_jobs = _run_gui(tracks, tmp_path / "gui", monkeypatch, app)
        cli_jobs = _run_cli(tracks, tmp_path / "cli", monkeypatch)

        gui_jobs[0][1].url_resolver(None)
        cli_jobs[0][1].url_resolver(None)

        assert len(resolver_calls) == 2
        assert resolver_calls[0] == resolver_calls[1]
