#!/usr/bin/env python3
"""
cli.py  –  Headless CLI for BananaFlow
===============================================
Downloads audio/video from YouTube, Spotify, and other supported sites
from the command line — no GUI, no Qt.

Uses the same core engine as the desktop app:
  PlaylistParser → DownloadOrchestrator → DownloadEngine

Usage
-----
    # Single track
    python cli.py "https://www.youtube.com/watch?v=TESTVIDEOAAA"

    # Playlist (downloads all tracks)
    python cli.py "https://www.youtube.com/playlist?list=PLxxxxx"

    # Spotify album → YouTube match → download
    python cli.py "https://open.spotify.com/album/TESTALBUMID00001"

    # Options
    python cli.py URL --media-type video --quality video_720 --output ~/Music
    python cli.py URL --audio-format flac --parallel 4
    python cli.py URL --cookies cookies.txt

    # List available tracks without downloading
    python cli.py URL --list

Run ``python cli.py --help`` for full options.
"""

from __future__ import annotations

import argparse
import sys
import os
from pathlib import Path

from utils.security import redact_text

# On Windows, the Playwright browser is bundled inside the EXE folder.
# On macOS, Chromium is bundled as loose files (chrome-mac directory) inside
# the .app to avoid nested .app re-signing issues. Point Playwright there.
if getattr(sys, 'frozen', False):
    if sys.platform == 'win32':
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(sys._MEIPASS) / 'ms-playwright')
    elif sys.platform == 'darwin':
        # Chromium lives in Contents/Resources/ms-playwright (not Contents/MacOS/)
        # so codesign does not scan it when sealing our main executables.
        _resources = Path(sys._MEIPASS).parent / 'Resources'
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(_resources / 'ms-playwright')

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

# Bootstrap logging before any project import
from utils.logging_config import setup_logging

if TYPE_CHECKING:
    from core.batch_outcome import BatchOutcome
    from core.batch_progress import BatchSnapshot


# ──────────────────────────────────────────────────────────────────────────────
# Terminal callbacks (implements OrchestratorCallbacks protocol)
# ──────────────────────────────────────────────────────────────────────────────

class TerminalCallbacks:
    """Prints progress to stderr so stdout stays clean for piping."""

    def __init__(self, total: int, quiet: bool = False) -> None:
        self._total = total
        self._quiet = quiet
        self._completed = 0
        self._failed = 0
        self._lock = threading.Lock()

    def on_track_progress(self, key: str, fraction: float) -> None:
        if self._quiet:
            return
        pct = int(fraction * 100)
        bar_w = 25
        filled = int(bar_w * fraction)
        bar = "█" * filled + "░" * (bar_w - filled)
        print(f"\r  [{bar}] {pct:>3}%", end="", flush=True, file=sys.stderr)

    def on_track_first_byte(self, key: str) -> None:
        """CLI has no separate first-byte display; satisfy the callback contract."""
        pass

    def on_track_speed(self, key: str, speed_bps: float, eta_seconds: float) -> None:
        # Live per-track speed/ETA drives the Qt UI's per-card display.
        # on_metrics() below is this class's speed/eta *string* hook, but
        # DownloadOrchestrator only ever calls it once, with blank strings,
        # at batch end (run_batch()'s finalisation step) — there is no
        # live formatted speed/eta to print here today. Intentional no-op.
        pass

    def on_track_status(self, key: str, status: str) -> None:
        pass  # handled by finished/error

    def on_track_finished(self, key: str, output_path: str) -> None:
        with self._lock:
            self._completed += 1
            n = self._completed
        name = Path(output_path).name if output_path else "unknown"
        print(f"\r  ✅  [{n}/{self._total}] {name}", file=sys.stderr)

    def on_track_preexisting(self, key: str, output_path: str) -> None:
        # Duplicate-skip: the file already existed, nothing was downloaded —
        # still a terminal success for the running total (see
        # core.batch_progress.JobState.PREEXISTING).
        with self._lock:
            self._completed += 1
            n = self._completed
        name = Path(output_path).name if output_path else "unknown"
        print(f"\r  ⏭️  [{n}/{self._total}] {name} (already exists)", file=sys.stderr)

    def on_track_error(self, key: str, error) -> None:
        with self._lock:
            self._failed += 1
        headline = redact_text(getattr(error, "headline", str(error)))
        print(f"\r  ❌  {key}: {headline}", file=sys.stderr)

    def on_track_phase(self, key: str, phase: str, remaining_seconds) -> None:
        # no-op, not an oversight - the terminal prints one line per track
        # rather than a live per-track bar, so intra-track stages have nowhere
        # to go. Same precedent as on_overall_progress below.
        pass

    def on_overall_progress(self, fraction: float) -> None:
        pass

    def on_metrics(self, speed: str, eta: str) -> None:
        if self._quiet or not speed:
            return
        print(f"  {speed}  {eta}    ", end="", flush=True, file=sys.stderr)

    def on_batch_snapshot(self, snapshot: "BatchSnapshot") -> None:
        # Aggregate batch progress (totals, weighted %, speed, ETA) is
        # already covered here by the per-track progress bar
        # (on_track_progress) plus the running ✅/❌ counters in
        # on_track_finished/on_track_error, which is what a piped,
        # single-line terminal stream can usefully show. Intentional
        # no-op, not an oversight — same precedent as on_overall_progress
        # above.
        pass

    def on_status_message(self, msg: str) -> None:
        if not self._quiet:
            print(f"\n{redact_text(msg)}", file=sys.stderr)

    def on_job_count_changed(self, completed: int, total: int) -> None:
        # Same "done" count this class already tracks itself via
        # on_track_finished/on_track_error (self._completed/self._failed)
        # and prints inline with each track. Intentional no-op.
        pass

    def on_batch_finished(self, outcome: "BatchOutcome") -> None:
        pass

    def on_track_thumbnail(self, key: str, thumbnail_url: str) -> None:
        # Qt-card-specific (refreshes a queue card's artwork); meaningless
        # in a headless terminal. Intentional no-op.
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Argument parser
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    from version import FULL_VERSION, PRODUCT_NAME

    p = argparse.ArgumentParser(
        prog="bananaflow-cli",
        description=f"{PRODUCT_NAME} — headless CLI mode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s https://youtu.be/TESTVIDEOAAA\n"
            "  %(prog)s https://youtube.com/playlist?list=PLxxx --media-type video\n"
            "  %(prog)s https://open.spotify.com/album/xxx --audio-format flac\n"
            "  %(prog)s URL --list\n"
            "  %(prog)s --version\n"
            "  %(prog)s --doctor\n"
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {FULL_VERSION}",
    )
    p.add_argument(
        "--doctor",
        action="store_true",
        help=(
            "Run startup diagnostics (FFmpeg, network, output dir, "
            "cookies, Playwright) and exit. URL not required."
        ),
    )
    p.add_argument(
        "--internal-smoke-test",
        choices=["converter"],
        default=None,
        help=argparse.SUPPRESS,   # packaged-build self-test; not a user feature
    )
    # URL is optional so --version / --doctor work without it. main()
    # enforces the requirement once the early-exit flags are handled.
    p.add_argument(
        "url",
        nargs="?",
        help="YouTube, Spotify, or supported URL",
    )
    p.add_argument(
        "-o", "--output",
        default=str(Path.home() / "Downloads" / "BananaFlow"),
        help="Output directory (default: ~/Downloads/BananaFlow)",
    )
    p.add_argument(
        "-f", "--media-type",
        choices=["audio", "video"],
        default="audio",
        help="Media type (default: audio)",
    )
    p.add_argument(
        "--audio-format",
        choices=["mp3", "m4a", "flac", "opus"],
        default="mp3",
        help="Audio output format, audio mode only (default: mp3)",
    )
    p.add_argument(
        "--quality",
        default=None,
        help=(
            "Quality preset ID or legacy label. Examples: audio_mp3_320, "
            "audio_m4a_256, video_1080, video_best, video_smallest."
        ),
    )
    p.add_argument(
        "--parallel", "-j",
        type=int, default=3,
        help="Concurrent downloads (1-6, default: 3)",
    )
    p.add_argument(
        "--cookies",
        default=None,
        help="Path to cookies.txt (Netscape format)",
    )
    p.add_argument(
        "--list", "-l",
        action="store_true",
        help="List tracks without downloading",
    )
    p.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress output (errors still shown)",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging to console",
    )
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Quality maps (mirrors AppWindow logic)
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_quality(label: str | None, is_audio: bool, audio_format: str):
    from core.quality_presets import (
        DEFAULT_VIDEO_QUALITY_ID,
        default_audio_quality_id_for_codec,
        parse_audio_quality_for_cli,
        parse_video_quality_for_cli,
    )

    if is_audio:
        return parse_audio_quality_for_cli(
            label or default_audio_quality_id_for_codec(audio_format),
            audio_format,
        )
    return parse_video_quality_for_cli(label or DEFAULT_VIDEO_QUALITY_ID)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def _run_doctor(args) -> int:
    """Print preflight diagnostics and exit.

    Does not require a URL and never raises. Returns 0 if every
    blocking check passes (FFmpeg, network, output dir); 1 otherwise.
    Informational checks (Playwright, cookies file) print their state
    but do not change the exit code.

    Deliberately English-only, by design, not oversight: this prints
    fixed-width tabular diagnostic lines (PreflightResult.detail_text(),
    YoutubeDoctorReport.summary_text()) meant for a console/log, not the
    friendly translated prose the GUI shows for the same checks (see
    ui.dialogs.youtube_doctor_dialog.build_report_text and
    ui.i18n.render_preflight_warnings, which ARE fully localized). Two
    concrete reasons this stays English rather than reading the user's
    saved language from AppConfig:
      1. Most terminals (especially Windows cmd/PowerShell) render
         Hebrew RTL text poorly or not at all, and it would break the
         fixed-width column alignment these lines depend on.
      2. `--doctor` is a developer/support diagnostic tool (its own
         docstring says "diagnostics") — the GUI's YouTube Doctor button
         is the localized, user-facing equivalent of this same data.
    """
    from error_handler import run_preflight
    from core.youtube_doctor import run_youtube_doctor
    from version import FULL_VERSION, PRODUCT_NAME

    output_dir = args.output if args.output else ""
    cookies_file = args.cookies if args.cookies else ""

    print(f"{PRODUCT_NAME} v{FULL_VERSION}  —  diagnostics")
    print("=" * 60)
    result = run_preflight(output_dir=output_dir, cookies_file=cookies_file)
    print(redact_text(result.detail_text()))
    print("=" * 60)

    print()
    print("YouTube Doctor")
    print("=" * 60)
    # CLI has no --cookies-browser flag and no --youtube-fast flag today,
    # so this reflects exactly what a CLI download would actually use.
    yt_report = run_youtube_doctor(cookies_file=cookies_file, youtube_reliability_mode="conservative")
    print(redact_text(yt_report.summary_text()))
    print("=" * 60)

    if result.all_ok():
        print("All blocking checks PASSED.")
        if result.warnings:
            print()
            print("Informational warnings:")
            print(redact_text(result.warning_text()))
        return 0
    print("FAILED — at least one blocking check did not pass:")
    print()
    print(redact_text(result.warning_text()))
    return 1


def main() -> int:
    args = build_parser().parse_args()
    setup_logging(debug=args.debug)
    logger = logging.getLogger("cli")

    # Activate downloader components bundled with this build (PO Token
    # Provider plugin + JS runtime) before any yt-dlp use or doctor run.
    try:
        from core.runtime_components import activate_bundled_components
        activate_bundled_components()
    except Exception:
        logger.warning("Bundled-component activation failed (non-fatal)", exc_info=True)

    # ── 0. Early-exit flags ──────────────────────────────────────────────
    # --version is handled by argparse before this point.
    if args.internal_smoke_test == "converter":
        from core.converter_smoke import run_converter_smoke
        return run_converter_smoke()
    if args.doctor:
        return _run_doctor(args)

    # URL is required for every other path. argparse made it optional so
    # --version / --doctor could run without it; enforce the requirement
    # here with a friendly error.
    if not args.url:
        print(
            "error: URL is required (use --version or --doctor for "
            "no-URL operations)",
            file=sys.stderr,
        )
        return 2

    # ── 1. Parse URL → track list ─────────────────────────────────────────
    from core.playlist_parser import PlaylistParser, TrackMeta, classify_url, SourcePlatform

    platform, kind = classify_url(args.url)
    if platform == SourcePlatform.UNKNOWN:
        print(f"❌  Unsupported URL: {redact_text(args.url)}", file=sys.stderr)
        return 1

    print(f"🔍  Resolving: {redact_text(args.url)}", file=sys.stderr)
    print(f"    Platform: {platform.name}  Kind: {kind.name}", file=sys.stderr)

    parser = PlaylistParser()
    tracks_seen: list[TrackMeta] = []

    def on_item(track: TrackMeta, idx: int, total) -> None:
        tracks_seen.append(track)
        total_str = str(total) if total else "?"
        if not args.quiet:
            print(
                f"  [{idx:>3}/{total_str}]  {track.artist[:20]:<20}  "
                f"{track.title[:45]:<45}  {track.duration_str}",
                file=sys.stderr,
            )

    result = parser.parse(
        args.url,
        cookies_file=args.cookies,
        on_item=on_item,
        on_progress=lambda msg: (
            print(f"  ℹ  {redact_text(msg)}", file=sys.stderr) if not args.quiet else None
        ),
        on_error=lambda msg: print(f"  ⚠  {redact_text(msg)}", file=sys.stderr),
    )

    if not result.tracks:
        print(f"❌  No tracks found. {redact_text(result.error or '')}", file=sys.stderr)
        return 1

    print(f"\n📋  {result.summary()}", file=sys.stderr)

    # ── 2. List-only mode ─────────────────────────────────────────────────
    if args.list:
        print()  # blank line for readability
        for t in result.tracks:
            # stdout: machine-parseable output
            print(
                f"{t.index}\t{t.artist}\t{t.title}\t{t.duration_str}\t"
                f"{redact_text(t.url)}"
            )
        return 0

    # ── 3. Build download jobs ────────────────────────────────────────────
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.spotify_request_builder import attach_spotify_matching, is_downloadable

    media_type = MediaType(args.media_type)
    is_audio   = media_type == MediaType.AUDIO
    quality    = _resolve_quality(args.quality, is_audio, args.audio_format)
    output_dir = args.output

    Path(output_dir).expanduser().mkdir(parents=True, exist_ok=True)

    engine = DownloadEngine()
    jobs: list[tuple[str, DownloadRequest]] = []

    playlist_name = result.playlist_title if len(result.tracks) > 1 else None

    # A track the scrape could not build usable metadata for has no target and
    # never will — a Spotify item that failed validation carries an empty URL.
    # The desktop app drops these before building requests; the CLI used to
    # hand yt-dlp the empty URL and report a download failure per track.
    # Same admission rule for both front-ends (issue #59).
    playable: list[TrackMeta] = []
    for track in result.tracks:
        if is_downloadable(track):
            playable.append(track)
            continue
        print(
            f"  ⚠  Skipping (no usable metadata): "
            f"{track.artist[:20]} — {track.title[:45]}",
            file=sys.stderr,
        )
    if not playable:
        print("❌  No track has usable metadata to download.", file=sys.stderr)
        return 1

    for track in playable:
        key = f"track-{track.index}"
        req_kwargs = dict(
            url=track.url,
            output_dir=output_dir,
            media_type=media_type,
            audio_format=args.audio_format,
            embed_thumbnail=True,
            embed_metadata=True,
            forced_title=track.title,
            forced_artist=track.artist,
            # Spotify only: TrackMeta.album carries the *playlist* title for
            # YouTube sources, so forcing it there would stamp a playlist name
            # into the album tag of every YouTube download. On Spotify it is
            # the real album name, which is what the GUI writes.
            forced_album=(
                track.album if track.platform == SourcePlatform.SPOTIFY else None
            ),
            forced_index=track.index if playlist_name else None,
            forced_duration=track.duration_sec,
            playlist_name=playlist_name,
            cookies_file=args.cookies,
            # Only reaches the history record (per-platform stats): the one
            # other platform-dependent branch, thumbnail cropping, additionally
            # requires square_thumbnails + thumbnail_url, neither of which the
            # CLI sets. Without this every CLI download was recorded "unknown".
            platform=track.platform,
        )
        if is_audio:
            req_kwargs["audio_quality"] = quality
        else:
            req_kwargs["video_quality"] = quality

        req = DownloadRequest(**req_kwargs)
        # Stage 2 of the Spotify two-stage import. Without this the CLI shipped
        # the parser's `ytsearch1:` placeholder straight to yt-dlp, downloading
        # the first free-text search hit while bypassing the scorer, the
        # album-aware search chain and the persistent match cache the GUI uses.
        attach_spotify_matching(req, track, args.cookies)
        jobs.append((key, req))

    # ── 4. Run orchestrator ───────────────────────────────────────────────
    from core.download_orchestrator import DownloadOrchestrator
    from core.history_db import HistoryDB

    db = HistoryDB()  # default path
    cb = TerminalCallbacks(total=len(jobs), quiet=args.quiet)

    orch = DownloadOrchestrator(
        engine=engine,
        callbacks=cb,
        db=db,
        max_workers=max(1, min(6, args.parallel)),
    )

    print(
        f"\n⬇  Downloading {len(jobs)} track(s) "
        f"({args.media_type}, {args.quality or 'default quality'}, {args.parallel} threads)…\n",
        file=sys.stderr,
    )

    batch = orch.run_batch(jobs)

    # ── 5. Summary ────────────────────────────────────────────────────────
    db.close()

    print(file=sys.stderr)
    if batch.cancelled:
        print("🚫  Cancelled.", file=sys.stderr)
        return 130
    if batch.failed > 0:
        print(
            f"⚠  Done with errors: {batch.completed} succeeded, "
            f"{batch.failed} failed.",
            file=sys.stderr,
        )
        return 1

    print(f"✅  All {batch.completed} track(s) downloaded.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    # See the note in main.py: mandatory for a frozen build so a
    # multiprocessing child never re-runs this entry point.
    import multiprocessing
    multiprocessing.freeze_support()

    sys.exit(main())
