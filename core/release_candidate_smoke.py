"""Non-interactive release-candidate checks for the frozen Windows app.

This module is reached only through BananaFlow's existing hidden
``--internal-smoke-test`` entry point.  It exercises production configuration,
cookie-storage and orchestration code from inside the packaged executable;
network transport is replaced with a small controlled engine so the release
gate is deterministic and safe to run unattended.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace


_SCENARIOS = frozenset({"fresh", "upgrade", "restart", "delete"})

_SPOTIFY_TRACK_FIXTURES = {
    "4CF4nWNKzvRsFN542wXLyX": {
        "type": "track", "name": "באת לי פתאום",
        "uri": "spotify:track:4CF4nWNKzvRsFN542wXLyX",
        "id": "4CF4nWNKzvRsFN542wXLyX",
        "artists": [{"name": "Keren Peles"}, {"name": "Roni Alter"}],
        "duration": 197100,
        "visualIdentity": {"image": [
            {"url": "https://image.spotify.invalid/hebrew-300.jpg", "maxWidth": 300, "maxHeight": 300},
            {"url": "https://image.spotify.invalid/hebrew-640.jpg", "maxWidth": 640, "maxHeight": 640},
        ]},
    },
    "2VxeLyX666F8uXCJ0dZF8B": {
        "type": "track", "name": "Shallow",
        "uri": "spotify:track:2VxeLyX666F8uXCJ0dZF8B",
        "id": "2VxeLyX666F8uXCJ0dZF8B",
        "artists": [{"name": "Lady Gaga"}, {"name": "Bradley Cooper"}],
        "duration": 215733,
        "visualIdentity": {"image": [
            {"url": "https://image.spotify.invalid/shallow-300.jpg", "maxWidth": 300, "maxHeight": 300},
            {"url": "https://image.spotify.invalid/shallow-640.jpg", "maxWidth": 640, "maxHeight": 640},
        ]},
    },
}


def _step(steps: list[dict], name: str, ok: bool, detail: str = "") -> None:
    steps.append({"step": name, "ok": bool(ok), "detail": detail})


def _write_result(result: dict) -> None:
    payload = json.dumps(result, indent=2)
    result_path = os.environ.get("BANANAFLOW_SMOKE_RESULT_FILE")
    if result_path:
        Path(result_path).write_text(payload, encoding="utf-8")
    try:
        print(payload)
    except Exception:
        pass


def _cookie_line(name: str, value: str) -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}"


class _Resolver:
    resolve_source = "live"

    def __init__(self, url: str) -> None:
        self.url = url
        self.calls = 0

    def __call__(self, cancel: threading.Event) -> str:
        self.calls += 1
        return "" if cancel.wait(0.03) else self.url


class _Engine:
    """Controlled transport that still drives real orchestrator callbacks."""

    def __init__(self, output_dir: Path) -> None:
        self._cancel_event = threading.Event()
        self.output_dir = output_dir
        self.urls: list[str] = []

    def cancel_all(self) -> None:
        self._cancel_event.set()

    def download(self, request) -> None:
        from core.downloader import DownloadProgress, DownloadStatus

        self.urls.append(request.url)
        index = int(request.url.rsplit("/", 1)[-1])
        total = 20_000 + index * 1_000
        started = time.monotonic()
        for part in range(1, 5):
            time.sleep(0.055)
            done = total * part // 4
            if request.on_progress:
                request.on_progress(DownloadProgress(
                    status=DownloadStatus.DOWNLOADING,
                    url=request.url,
                    downloaded_bytes=done,
                    total_bytes=total,
                    speed_bps=done / max(0.001, time.monotonic() - started),
                    fraction=part / 4,
                ))
        output = self.output_dir / f"rc-smoke-{index}.bin"
        output.write_bytes(b"x" * total)
        if request.on_finished:
            request.on_finished(DownloadProgress(
                status=DownloadStatus.FINISHED,
                url=request.url,
                output_path=str(output),
                downloaded_bytes=total,
                total_bytes=total,
                fraction=1.0,
            ))


class _Callbacks:
    def __init__(self) -> None:
        self.snapshots: list[object] = []

    def on_batch_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)

    def __getattr__(self, name: str):
        if name.startswith("on_"):
            return lambda *_args, **_kwargs: None
        raise AttributeError(name)


def _orchestrator_checks(steps: list[dict], output_dir: Path) -> None:
    from core.downloader import DownloadRequest, MediaType
    from core.download_orchestrator import DownloadOrchestrator

    output_dir.mkdir(parents=True, exist_ok=True)
    engine = _Engine(output_dir)
    callbacks = _Callbacks()
    resolvers: list[_Resolver] = []
    jobs = []
    for index in range(6):
        real_url = f"https://trace.invalid/release/{index}"
        request = DownloadRequest(
            url=real_url,
            output_dir=str(output_dir),
            media_type=MediaType.AUDIO,
            forced_title=f"release-smoke-{index}",
        )
        if index >= 3:
            resolver = _Resolver(real_url)
            resolvers.append(resolver)
            request.url = f"spotify:release-smoke:{index}"
            request.url_resolver = resolver
        jobs.append((f"release-smoke-{index}", request))

    result = DownloadOrchestrator(
        engine, callbacks, max_workers=1,
    ).run_batch(jobs, delay_range=(0.0, 0.0), batch_id="release-candidate")
    _step(
        steps,
        "direct_download_startup",
        result.completed == 6 and any("/release/0" in url for url in engine.urls),
        f"completed={result.completed} failed={result.failed}",
    )
    _step(
        steps,
        "spotify_resolver_startup",
        bool(resolvers) and all(resolver.calls == 1 for resolver in resolvers),
        f"resolver_calls={sum(resolver.calls for resolver in resolvers)}",
    )
    eta_snapshots = [
        snapshot for snapshot in callbacks.snapshots
        if getattr(snapshot, "eta_seconds", None) is not None
    ]
    _step(
        steps,
        "eta_snapshot_delivery",
        bool(eta_snapshots),
        f"snapshots={len(callbacks.snapshots)} eta_snapshots={len(eta_snapshots)}",
    )

    # A failed Spotify resolver is terminal before DownloadEngine.  A resolved
    # peer in the same batch must still finish, proving both mixed-batch
    # continuation and the empty-URL boundary in packaged production code.
    mixed_engine = _Engine(output_dir)
    mixed_callbacks = _Callbacks()
    valid = DownloadRequest(
        url="https://trace.invalid/release/9",
        output_dir=str(output_dir), media_type=MediaType.AUDIO,
    )
    invalid = DownloadRequest(
        url="ytsearch1:invalid spotify metadata",
        output_dir=str(output_dir), media_type=MediaType.AUDIO,
    )
    invalid.spotify_match_identity = {"title": "Song", "artist": "Artist"}
    invalid.url_resolver = _Resolver("")
    mixed = DownloadOrchestrator(
        mixed_engine, mixed_callbacks, max_workers=2,
    ).run_batch(
        [("resolved", valid), ("unresolved", invalid)],
        delay_range=(0.0, 0.0), batch_id="release-candidate-mixed",
    )
    _step(
        steps, "mixed_resolved_unresolved_batch",
        mixed.completed == 1 and mixed.failed == 1,
        f"completed={mixed.completed} failed={mixed.failed}",
    )
    _step(
        steps, "no_empty_url_engine_submission",
        mixed_engine.urls == ["https://trace.invalid/release/9"]
        and all(mixed_engine.urls),
        f"engine_urls={len(mixed_engine.urls)}",
    )


def _spotify_production_checks(steps: list[dict], output_dir: Path) -> None:
    """Exercise frozen real Spotify metadata through production parsing/scoring."""
    from core.match_errors import SpotifyMetadataInvalid
    from utils.spotify_resolver import (
        normalise_spotify_artist_credits,
        parse_spotify_embed_track_html,
    )

    parsed = {}
    for track_id, entity in _SPOTIFY_TRACK_FIXTURES.items():
        payload = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload, ensure_ascii=False)
            + "</script>"
        )
        parsed[track_id] = parse_spotify_embed_track_html(html, track_id)
    _step(
        steps, "spotify_track_scoped_metadata",
        parsed["4CF4nWNKzvRsFN542wXLyX"]["artist_credits"]
        == ["Keren Peles", "Roni Alter"]
        and parsed["2VxeLyX666F8uXCJ0dZF8B"]["artist_credits"]
        == ["Lady Gaga", "Bradley Cooper"],
        "two frozen production track responses parsed",
    )
    _step(
        steps, "spotify_track_scoped_artwork",
        parsed["4CF4nWNKzvRsFN542wXLyX"]["thumbnail_url"].endswith("hebrew-640.jpg")
        and parsed["2VxeLyX666F8uXCJ0dZF8B"]["thumbnail_url"].endswith("shallow-640.jpg"),
    )

    malformed_detected = False
    try:
        normalise_spotify_artist_credits([
            "Lady Gaga", "Lady Gaga", "Bradley Cooper",
            "Popular Releases by Lady Gaga", "Show all",
        ])
    except SpotifyMetadataInvalid:
        malformed_detected = True
    _step(steps, "spotify_polluted_metadata_detected", malformed_detected)

    import core.spotify_match_scorer as scorer
    import core.scraper as scraper
    import ytmusicapi

    original_ytmusic = ytmusicapi.YTMusic
    original_search = scorer._search
    original_deep = scorer._deep_validate_urls
    fallback_calls: list[str] = []
    try:
        class _StrictYTM:
            def search(self, *_args, **_kwargs):
                return [{
                    "videoId": "strict01", "title": "Shallow",
                    "artists": [{"name": "Lady Gaga"}, {"name": "Bradley Cooper"}],
                    "duration_seconds": 215,
                }]

        ytmusicapi.YTMusic = _StrictYTM
        scorer._search = lambda *_a, **_k: fallback_calls.append("unexpected") or []
        strict_url = scraper._resolve_to_ytm_url(
            "Shallow", "Lady Gaga, Bradley Cooper", 215,
        )
        _step(
            steps, "spotify_strict_match_flow",
            strict_url.endswith("strict01") and not fallback_calls,
        )

        class _InconclusiveYTM:
            def search(self, *_args, **_kwargs):
                return [{
                    "videoId": "karaoke0", "title": "Shallow Karaoke",
                    "artists": [{"name": "Karaoke All Stars"}],
                    "duration_seconds": 215,
                }]

        candidates = [
            {"id": "tribute1", "title": "Lady Gaga & Bradley Cooper - Shallow",
             "channel": "Tribute Stage", "artists": [{"name": "Tribute Stage"}],
             "duration": 215},
            {"id": "official1", "title": "Shallow", "channel": "Lady Gaga",
             "artists": [{"name": "Lady Gaga"}, {"name": "Bradley Cooper"}],
             "duration": 215},
        ]
        fallback_calls.clear()
        ytmusicapi.YTMusic = _InconclusiveYTM
        scorer._search = lambda *_a, **_k: fallback_calls.append("general") or list(candidates)
        scorer._deep_validate_urls = lambda *_a, **_k: []
        fallback_url = scraper._resolve_to_ytm_url(
            "Shallow", "Lady Gaga, Bradley Cooper", 215,
        )
        _step(
            steps, "spotify_general_fallback_flow",
            fallback_calls == ["general"] and fallback_url.endswith("official1"),
            f"fallback_calls={len(fallback_calls)}",
        )
    finally:
        ytmusicapi.YTMusic = original_ytmusic
        scorer._search = original_search
        scorer._deep_validate_urls = original_deep

    # UI state is exercised without showing a window.  An invalid row starts
    # unselected/non-downloadable and therefore leaves Download disabled.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from ui.app_window import _DownloadBar
    from ui.components.track_card import TrackCard

    app = QApplication.instance() or QApplication([])
    card = TrackCard(
        "Broken Spotify row", platform="spotify", track_url="",
        match_status="metadata_invalid",
        resolution_error="spotify_metadata_invalid_card",
    )
    bar = _DownloadBar()
    bar.set_count(0, 1)
    _step(
        steps, "spotify_unresolved_ui_state",
        not card.is_selected() and not card.is_downloadable()
        and not bar._dl_btn.isEnabled(),
    )
    card.deleteLater()
    bar.deleteLater()
    app.processEvents()

    # Follow the user-visible artwork/source contract through real queue cards
    # and DownloadController request construction.  Two direct Spotify URLs
    # must stay root-level and unnumbered; one album item remains grouped even
    # when it is the only selected card.
    from config import AppConfig
    from core.playlist_parser import UrlKind
    from ui.controllers.download_controller import DownloadController

    cfg = AppConfig()
    cfg.output_dir = str(output_dir)
    cfg.duplicate_action = "overwrite"
    cfg.playlist_subfolders = True
    cfg.playlist_index_prefix = True
    controlled = _Engine(output_dir)
    controller = DownloadController(cfg, controlled)
    built = []

    class _NoopWorker:
        def start(self):
            pass

    controller._build_batch_worker = (  # noqa: SLF001 - packaged smoke seam
        lambda jobs, preexisting: built.extend(jobs) or _NoopWorker()
    )
    direct_cards = [
        TrackCard(
            meta["title"], meta["artist"], queue_index=index,
            platform="spotify", track_url=f"https://trace.invalid/release/{index}",
            parent_artist=meta["artist_credits"][0], release_type="single",
            thumbnail_url=meta["thumbnail_url"],
            source_kind=UrlKind.SINGLE_VIDEO.name,
            source_url=f"https://open.spotify.com/track/{track_id}",
        )
        for index, (track_id, meta) in enumerate(parsed.items(), start=1)
    ]
    opts = {
        "media_type": "audio", "quality_label": "320",
        "audio_format": "mp3", "video_format": "mp4",
        "output_dir": str(output_dir),
    }
    controller.start_batch(direct_cards, opts, UrlKind.SINGLE_VIDEO, "")
    direct_requests = [request for _key, request in built]
    _step(
        steps, "independent_track_output_context",
        len(direct_requests) == 2
        and all(request.playlist_name in (None, "") for request in direct_requests)
        and all(request.forced_index is None and request.is_solo for request in direct_requests),
    )
    _step(
        steps, "spotify_artwork_download_request",
        [request.thumbnail_url for request in direct_requests]
        == [card.thumbnail_url for card in direct_cards]
        and all(request.thumbnail_url for request in direct_requests),
    )

    built.clear()
    album_card = TrackCard(
        "Album Track", "Album Artist", queue_index=8, platform="spotify",
        track_url="https://trace.invalid/release/8", parent_artist="Album Artist",
        album="Album Name", release_type="album", album_index=3,
        source_kind=UrlKind.ALBUM.name,
        source_url="https://open.spotify.com/album/album-id",
    )
    controller.start_batch([album_card], opts, UrlKind.SINGLE_VIDEO, "Wrong Global")
    album_request = built[0][1]
    _step(
        steps, "grouped_source_output_context",
        "Album Name" in (album_request.playlist_name or "")
        and album_request.forced_index == 3 and not album_request.is_solo,
    )

    for queue_card in [*direct_cards, album_card]:
        queue_card.deleteLater()
    app.processEvents()


def _txt_import_checks(steps: list[dict], app_data: Path) -> None:
    """Drive the packaged FetchController over a realistic three-URL TXT."""
    from PySide6.QtCore import QObject, Signal
    from config import AppConfig
    from core.playlist_parser import ParseResult, SourcePlatform, TrackMeta, UrlKind
    from ui.controllers.fetch_controller import FetchController
    import ui.workers.fetch_worker as worker_module

    urls = [f"https://youtu.be/rcsmoke0000{index}" for index in range(1, 4)]
    batch_file = app_data / "release-smoke-urls.txt"
    batch_file.write_text("\n".join([urls[0], "unsupported text", *urls[1:]]), encoding="utf-8")
    starts: list[str] = []
    emitted: list[dict] = []
    summaries: list[str] = []
    original_worker = worker_module.FetchWorker

    class _SmokeFetchWorker(QObject):
        track_found = Signal(dict, int, int)
        progress_msg = Signal(str)
        soft_error = Signal(str)
        finished = Signal(object)
        error = Signal(object)

        def __init__(self, url, **_kwargs):
            super().__init__()
            self.url = url

        def start(self):
            starts.append(self.url)
            if self.url == urls[1]:
                self.error.emit("controlled per-URL failure")
                return
            meta = TrackMeta(
                title=self.url[-1], url=self.url, platform=SourcePlatform.YOUTUBE,
                source_kind=UrlKind.SINGLE_VIDEO.name, source_url=self.url,
            )
            self.track_found.emit({
                "title": meta.title, "track_url": meta.url,
                "source_kind": meta.source_kind, "source_url": meta.source_url,
            }, 1, 1)
            self.finished.emit(ParseResult(
                url=self.url, kind=UrlKind.SINGLE_VIDEO,
                platform=SourcePlatform.YOUTUBE, tracks=[meta], total_count=1,
            ))

        def isRunning(self):
            return False

        def cancel(self):
            pass

        def wait(self, _milliseconds):
            return True

    try:
        worker_module.FetchWorker = _SmokeFetchWorker
        controller = FetchController(AppConfig())
        controller.track_fetched.connect(emitted.append)
        controller.temporary_status.connect(summaries.append)
        controller.batch_import(str(batch_file))
    finally:
        worker_module.FetchWorker = original_worker
        batch_file.unlink(missing_ok=True)

    _step(
        steps, "txt_import_all_urls",
        starts == urls and [item["source_url"] for item in emitted] == [urls[0], urls[2]],
        f"started={len(starts)} succeeded={len(emitted)}",
    )
    _step(
        steps, "txt_import_failure_continuation",
        bool(summaries) and "2" in summaries[-1] and "1" in summaries[-1],
    )


def _provider_timeout_checks(steps: list[dict]) -> None:
    """Reproduce the packaged Deno TimeoutExpired circuit-breaker boundary."""
    import subprocess
    from yt_dlp.utils import Popen
    from utils import yt_dlp_opts

    original_run = Popen.run
    original_installed = yt_dlp_opts._bgutil_stderr_capture_installed  # noqa: SLF001
    calls = []

    def _timeout(command, *args, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 15.0)

    try:
        yt_dlp_opts.reset_po_token_provider_circuit()
        yt_dlp_opts._bgutil_stderr_capture_installed = False  # noqa: SLF001
        Popen.run = staticmethod(_timeout)
        yt_dlp_opts.install_bgutil_stderr_capture()
        for _ in range(2):
            try:
                Popen.run(["deno", "run", "generate_once.ts", "--version"])
            except subprocess.TimeoutExpired:
                pass
        metrics = yt_dlp_opts.po_token_provider_metrics()
        _step(
            steps, "provider_timeout_work_bounded",
            len(calls) == 2 and metrics["attempts"] == 2 and metrics["circuit_open"],
            f"attempts={metrics['attempts']}",
        )
    finally:
        Popen.run = original_run
        yt_dlp_opts._bgutil_stderr_capture_installed = original_installed  # noqa: SLF001
        yt_dlp_opts.reset_po_token_provider_circuit()


def _verify_protected_store(steps: list[dict], secret: str) -> None:
    from utils.cookie_store import DPAPI_MAGIC, materialize_cookie_file, read_cookie_store
    from utils.paths import get_app_cookies_path, get_legacy_app_cookies_path

    protected = get_app_cookies_path()
    legacy = get_legacy_app_cookies_path()
    payload = protected.read_bytes() if protected.exists() else b""
    plaintext = read_cookie_store(protected) if protected.exists() else ""
    _step(
        steps,
        "dpapi_cookie_access",
        payload.startswith(DPAPI_MAGIC) and secret in plaintext,
        "protected store decrypted for the current Windows user",
    )
    _step(
        steps,
        "no_plaintext_persistent_cookie",
        not legacy.exists() and secret.encode("utf-8") not in payload,
        "legacy plaintext absent and protected bytes contain no canary",
    )

    temporary_path: Path | None = None
    with materialize_cookie_file(protected) as materialized:
        temporary_path = Path(materialized) if materialized else None
        usable = bool(
            temporary_path
            and temporary_path.exists()
            and secret in temporary_path.read_text(encoding="utf-8")
        )
        _step(steps, "temporary_cookie_access", usable)
    _step(
        steps,
        "temporary_cookie_cleanup",
        temporary_path is not None and not temporary_path.exists(),
    )


def run_release_candidate_smoke() -> int:
    """Run one phase selected by ``BANANAFLOW_RC_SMOKE_SCENARIO``."""
    from core.runtime_mode import set_internal_smoke

    set_internal_smoke(True)
    scenario = os.environ.get("BANANAFLOW_RC_SMOKE_SCENARIO", "").strip().lower()
    steps: list[dict] = []
    result = {"target": "release-candidate", "scenario": scenario, "ok": False, "steps": steps}
    secret = os.environ.get("BANANAFLOW_RC_COOKIE_SECRET", "")

    try:
        if os.name != "nt":
            raise RuntimeError("release-candidate smoke requires Windows")
        if scenario not in _SCENARIOS:
            raise RuntimeError(f"unknown release-candidate scenario: {scenario!r}")
        if not secret:
            raise RuntimeError("release-candidate cookie canary is missing")

        from config import AppConfig
        from utils.paths import (
            get_app_browser_profile_dir,
            get_app_cookies_path,
            get_app_data_dir,
            get_legacy_app_cookies_path,
        )

        app_data = get_app_data_dir()
        config_path = app_data / "config.json"
        cfg = AppConfig()
        _step(steps, "configuration_loaded", cfg._path == config_path)

        if scenario == "fresh":
            from utils.cookie_store import write_cookie_store

            cfg.output_dir = str(app_data / "smoke-downloads")
            cfg.cookies_file = str(get_app_cookies_path())
            cfg.save()
            write_cookie_store(
                get_app_cookies_path(),
                _cookie_line("LOGIN_INFO", secret)
                + "\n"
                + _cookie_line("SID", "excluded-broad-google-cookie")
                + "\n",
            )
            reloaded = AppConfig()
            _step(
                steps,
                "configuration_saved",
                reloaded.output_dir == cfg.output_dir
                and reloaded.cookies_file == cfg.cookies_file,
            )
            _verify_protected_store(steps, secret)
            _orchestrator_checks(steps, app_data / "smoke-downloads")
            _spotify_production_checks(steps, app_data / "smoke-downloads")
            _txt_import_checks(steps, app_data)
            _provider_timeout_checks(steps)

        elif scenario == "upgrade":
            _step(
                steps,
                "unsupported_chromium_migrated",
                cfg.cookies_browser == ""
                and cfg.cookies_browser_migration_notice_pending,
            )
            _step(
                steps,
                "legacy_plaintext_migrated",
                not get_legacy_app_cookies_path().exists()
                and Path(cfg.cookies_file) == get_app_cookies_path(),
            )
            _verify_protected_store(steps, secret)

            import ui.app_window as app_window_module

            notices: list[tuple[str, str]] = []
            original_show_info = app_window_module.show_info
            app_window_module.show_info = (
                lambda _parent, title, message: notices.append((title, message))
            )
            try:
                fake_window = SimpleNamespace(_cfg=cfg)
                app_window_module.AppWindow._show_browser_cookie_migration_notice(fake_window)
                app_window_module.AppWindow._show_browser_cookie_migration_notice(fake_window)
            finally:
                app_window_module.show_info = original_show_info
            reloaded = AppConfig()
            _step(
                steps,
                "one_time_migration_notice",
                len(notices) == 1
                and not reloaded.cookies_browser_migration_notice_pending,
                f"notice_count={len(notices)}",
            )
            _orchestrator_checks(steps, app_data / "smoke-downloads")

        elif scenario == "restart":
            _step(
                steps,
                "restart_after_migration",
                cfg.cookies_browser == ""
                and not cfg.cookies_browser_migration_notice_pending,
            )
            _verify_protected_store(steps, secret)
            residue = list((app_data / "auth_tmp").glob("session-*.txt"))
            _step(
                steps,
                "crash_residue_cleanup",
                not residue,
                f"remaining={len(residue)}",
            )
            cfg.check_updates = False
            cfg.save()
            _step(steps, "configuration_resaved_after_restart", not AppConfig().check_updates)

        else:  # delete
            from utils.security import delete_stored_auth_data

            profile = get_app_browser_profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            (profile / "smoke-profile-marker").write_text("owned", encoding="utf-8")
            deletion = delete_stored_auth_data()
            cfg.cookies_file = ""
            cfg.cookies_browser = ""
            cfg.save()
            reloaded = AppConfig()
            _step(
                steps,
                "stored_sign_in_deletion",
                deletion.success
                and not get_app_cookies_path().exists()
                and not get_legacy_app_cookies_path().exists()
                and not profile.exists()
                and reloaded.cookies_file == "",
                f"removed={','.join(deletion.removed)} failed={','.join(deletion.failed)}",
            )

        result["ok"] = all(step["ok"] for step in steps)
    except Exception as exc:  # noqa: BLE001 - smoke must report every failure
        _step(steps, "exception", False, f"{type(exc).__name__}: {exc}")
        result["traceback"] = traceback.format_exc()

    try:
        _write_result(result)
    except Exception:
        return 1
    return 0 if result["ok"] else 1
