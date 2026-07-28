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
