"""
tests/test_hls_downloader_cancel.py  –  Real ffmpeg process cancellation
============================================================================
core.hls_downloader.download_hls used to run ffmpeg via a blocking call
with no way to interrupt it — a user cancel during a long HLS/DASH remux
had no effect until ffmpeg finished on its own. This exercises a REAL
ffmpeg child process (skipped where ffmpeg/ffprobe aren't available,
matching the project's existing needs_ffmpeg convention) to prove:

  * a cancel_event set mid-download actually terminates the ffmpeg
    process (not just "the Python call returns eventually").
  * no ffmpeg process is left running after cancellation.
  * normal (non-cancelled) downloads are unaffected.
  * download_hls's own stderr capture (used for error messages) does not
    deadlock even though ffmpeg's "-stats" flag writes continuous
    progress lines — this was a real risk introduced by wiring in
    cancellation via a polling loop that must never block on a full,
    undrained OS pipe buffer.

Uses a local HTTP server (not a real network stream) so the test is fast,
offline, and deterministic; ffmpeg's -reconnect options require an actual
http:// URL (they no-op/fail against a bare local file path), so a static
file server is the accurate stand-in for a real remote stream.
"""

from __future__ import annotations

import http.server
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from utils.paths import get_ffmpeg_executable, get_ffprobe_executable

FFMPEG = get_ffmpeg_executable()
FFPROBE = get_ffprobe_executable()
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available",
)


def _running_ffmpeg_pids() -> set[int]:
    """Best-effort snapshot of ffmpeg PIDs currently running, so a test can
    confirm cancellation didn't leave one behind. Windows-only (tasklist);
    returns an empty set (skipping the assertion) elsewhere."""
    if os.name != "nt":
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ffmpeg.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        return set()
    pids = set()
    for line in out.stdout.splitlines():
        parts = [p.strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].lower() == "ffmpeg.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


class _ThrottledHandler(http.server.SimpleHTTPRequestHandler):
    """Serves files in small chunks with a short sleep between each — a
    localhost transfer of a small file is otherwise ~instant, which would
    make ffmpeg finish before a test ever gets a chance to cancel it
    mid-flight, regardless of the source's nominal audio duration."""

    CHUNK_SIZE = 4096
    CHUNK_DELAY_S = 0.05

    def copyfile(self, source, outputfile) -> None:  # noqa: N802 - BaseHTTPServer API name
        while True:
            chunk = source.read(self.CHUNK_SIZE)
            if not chunk:
                break
            outputfile.write(chunk)
            time.sleep(self.CHUNK_DELAY_S)

    def log_message(self, format, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # keep test output quiet


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    """A local static file server so ffmpeg's -reconnect options (which
    require a real http:// URL) work against a controllable, offline
    source — throttled so a long-enough file takes a real, reliably
    cancellable amount of time to transfer even over localhost."""
    directory = tmp_path_factory.mktemp("hls_http_root")

    handler = lambda *a, **k: _ThrottledHandler(
        *a, directory=str(directory), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, directory
    server.shutdown()
    thread.join(5.0)


@pytest.fixture(scope="module")
def long_test_source(http_server):
    """A 30-second synthetic sine-wave WAV, long enough that encoding to
    MP3 takes measurably longer than the cancellation poll interval."""
    _server, directory = http_server
    src = directory / "long_source.wav"
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
         "-c:a", "pcm_s16le", str(src)],
        check=True, capture_output=True,
    )
    return src.name


@pytest.fixture(scope="module")
def short_test_source(http_server):
    _server, directory = http_server
    src = directory / "short_source.wav"
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:a", "pcm_s16le", str(src)],
        check=True, capture_output=True,
    )
    return src.name


def _url(http_server, name: str) -> str:
    server, _directory = http_server
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}/{name}"


@needs_ffmpeg
class TestHlsCancellation:
    def test_cancel_mid_download_terminates_ffmpeg_promptly(
        self, tmp_path, http_server, long_test_source,
    ):
        from core.hls_downloader import download_hls, HlsCancelled

        before_pids = _running_ffmpeg_pids()
        cancel_event = threading.Event()

        def _cancel_soon():
            time.sleep(0.4)
            cancel_event.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()

        start = time.monotonic()
        with pytest.raises(HlsCancelled):
            download_hls(
                _url(http_server, long_test_source),
                str(tmp_path / "out.mp3"),
                cancel_event=cancel_event,
            )
        elapsed = time.monotonic() - start

        # Terminated promptly — nowhere near the full 30s the source runs.
        assert elapsed < 5.0, f"cancellation took {elapsed:.1f}s — ffmpeg was not interrupted"

        # No ffmpeg process left running as a result of this test.
        time.sleep(0.3)  # let the OS finish reaping the killed process
        leaked = _running_ffmpeg_pids() - before_pids
        assert not leaked, f"ffmpeg process(es) still running after cancel: {leaked}"

    def test_normal_completion_is_unaffected(self, tmp_path, http_server, short_test_source):
        from core.hls_downloader import download_hls

        out = tmp_path / "out_normal.mp3"
        result = download_hls(_url(http_server, short_test_source), str(out))

        assert result == str(out)
        assert out.exists()
        assert out.stat().st_size > 0

    def test_stats_output_does_not_deadlock_the_poll_loop(
        self, tmp_path, http_server, long_test_source,
    ):
        """ffmpeg's "-stats" flag writes a progress line roughly every 0.5s.
        If stderr were captured via an undrained PIPE, a long-enough
        download would fill the OS pipe buffer and hang forever. This must
        complete (via cancellation) well within a sane bound, not hang."""
        from core.hls_downloader import download_hls, HlsCancelled

        cancel_event = threading.Event()

        def _cancel_after_stats_have_had_time_to_pile_up():
            time.sleep(2.0)  # several -stats lines will have been written by now
            cancel_event.set()

        threading.Thread(target=_cancel_after_stats_have_had_time_to_pile_up, daemon=True).start()

        start = time.monotonic()
        with pytest.raises(HlsCancelled):
            download_hls(
                _url(http_server, long_test_source),
                str(tmp_path / "out2.mp3"),
                cancel_event=cancel_event,
            )
        elapsed = time.monotonic() - start
        assert elapsed < 8.0, f"took {elapsed:.1f}s — looks like a pipe deadlock, not a clean cancel"
