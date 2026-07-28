"""
core/hls_downloader.py  –  ffmpeg-based HLS / DASH / direct-stream downloader
==============================================================================
Used when the universal_extractor has found a raw HLS (.m3u8) or DASH (.mpd)
URL that yt-dlp cannot handle (because the URL comes from interception, not
from a supported extractor).

Zero Qt imports.  Pure subprocess + stdlib.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from utils.proc import log_exit, popen_hidden, run_hidden
from utils.security import redact_text

logger = logging.getLogger(__name__)

# Supported ffmpeg output containers by extension
_AUDIO_FORMATS = {"mp3", "m4a", "aac", "flac", "opus", "wav", "ogg"}
_VIDEO_FORMATS = {"mp4", "mkv", "webm", "mov", "ts"}

# How often the cancel-polling loop checks the process/event, in seconds.
_POLL_INTERVAL_S = 0.25
# Grace period after terminate() before escalating to kill().
_TERMINATE_GRACE_S = 3.0


class HlsCancelled(Exception):
    """download_hls() was cancelled mid-download (cancel_event was set)."""


def _find_ffmpeg() -> str:
    """Return the ffmpeg executable path or the literal ``ffmpeg`` token.

    Single source of truth: ``utils.paths.get_ffmpeg_executable``
    chooses the bundled binary next to bananaflow.exe when present and
    falls back to PATH. If even PATH lookup fails we return the
    literal ``ffmpeg`` so the subsequent subprocess.run raises a
    clear FileNotFoundError with a friendly message.
    """
    from utils.paths import get_ffmpeg_executable
    return get_ffmpeg_executable() or "ffmpeg"


def _find_ffprobe() -> str:
    """Return ffprobe path next to the discovered ffmpeg."""
    from utils.paths import get_bundled_ffmpeg_dir
    bundled = get_bundled_ffmpeg_dir()
    if bundled is not None:
        suffix = ".exe" if Path(_find_ffmpeg()).suffix.lower() == ".exe" else ""
        fp = bundled / f"ffprobe{suffix}"
        if fp.exists():
            return str(fp)
    return "ffprobe"


def download_hls(
    url:           str,
    output_path:   str,
    cookies_file:  Optional[str]                             = None,
    headers:       Optional[dict[str, str]]                  = None,
    timeout_sec:   int                                       = 3600,
    on_progress:   Optional[Callable[[float, str, str], None]] = None,
    cancel_event:  Optional[threading.Event]                 = None,
) -> str:
    """
    Download an HLS/DASH/direct stream URL using ffmpeg.

    Parameters
    ----------
    url           : The HLS manifest, DASH manifest, or direct media URL.
    output_path   : Destination file path (extension determines container).
    cookies_file  : Netscape-format cookies.txt (optional).
    headers       : Extra HTTP headers dict (optional).
    timeout_sec   : Maximum wall-clock time before giving up.
    on_progress   : Callback(fraction, speed_str, eta_str).  fraction=-1 = unknown.
    cancel_event  : Checked every ~0.25s while ffmpeg runs. When set, the
                    ffmpeg child is terminated (SIGTERM, then SIGKILL after a
                    grace period) and HlsCancelled is raised — the caller
                    must not treat a cancelled remux as a completed
                    download.  Without this the process only stopped when
                    ffmpeg itself finished or the app process exited, so a
                    user cancel during a long stream had no effect until the
                    whole remux ran to completion.

    Returns
    -------
    output_path on success.  Raises RuntimeError on failure, HlsCancelled if
    cancel_event fired before ffmpeg exited.
    """
    ffmpeg = _find_ffmpeg()
    out    = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg,
        "-y",                     # overwrite without asking
        "-loglevel", "error",
        "-stats",                  # emit progress to stderr
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]

    # Cookies: ffmpeg uses a single "Cookie: k=v; ..." header value
    cookie_header = ""
    if cookies_file and Path(cookies_file).exists():
        from utils.cookie_store import read_cookie_store
        try:
            cookie_header = _netscape_text_to_cookie_header(read_cookie_store(cookies_file))
        except OSError:
            logger.debug("hls_downloader: protected cookie store could not be read")
        if cookie_header:
            cmd += ["-headers", f"Cookie: {cookie_header}\r\n"]

    # Extra headers (e.g. Referer, Origin)
    if headers:
        for k, v in headers.items():
            cmd += ["-headers", f"{k}: {v}\r\n"]

    cmd += ["-i", url, "-c", "copy"]

    # Audio-only output: strip video streams
    ext = out.suffix.lstrip(".").lower()
    if ext in _AUDIO_FORMATS:
        cmd += ["-vn"]
        if ext == "mp3":
            cmd += ["-acodec", "libmp3lame", "-q:a", "0"]
        elif ext in ("m4a", "aac"):
            cmd += ["-acodec", "aac"]
        elif ext == "flac":
            cmd += ["-acodec", "flac"]
        elif ext == "opus":
            cmd += ["-acodec", "libopus"]
        else:
            cmd += ["-acodec", "copy"]

    cmd.append(str(out))

    logger.debug(
        "hls_downloader: ffmpeg=%s input=%s output=%s cookies=%s extra_headers=%d",
        Path(ffmpeg).name,
        redact_text(url),
        out.name,
        bool(cookie_header) if cookies_file and Path(cookies_file).exists() else False,
        len(headers or {}),
    )

    start = time.monotonic()
    # Hidden launch: FFmpeg is a console program, and a windowed build has
    # no console to inherit, so Windows would give it a visible one.
    #
    # Popen + a polling loop (not the blocking run_hidden) so cancel_event
    # can actually interrupt a long-running remux instead of only being
    # checked once ffmpeg has already exited.
    #
    # stderr goes to a temp FILE, not a pipe: ffmpeg's "-stats" writes a
    # progress line every ~0.5s, and a PIPE nobody drains during the polling
    # loop below fills its OS buffer and deadlocks the child on write() —
    # a file has no such backpressure.
    stderr_fh = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = popen_hidden(
            cmd, purpose="hls-remux", log_command=False,
            stdout=subprocess.DEVNULL, stderr=stderr_fh,
        )
    except (OSError, ValueError):
        stderr_fh.close()
        raise RuntimeError(
            "ffmpeg not found.  Install ffmpeg and ensure it is on PATH."
        )

    cancelled = False
    timed_out = False
    while True:
        try:
            proc.wait(timeout=_POLL_INTERVAL_S)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            proc.terminate()
            try:
                proc.wait(timeout=_TERMINATE_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=_TERMINATE_GRACE_S)
            break
        if time.monotonic() - start > timeout_sec:
            timed_out = True
            proc.kill()
            proc.wait(timeout=_TERMINATE_GRACE_S)
            break

    try:
        stderr_fh.seek(0)
        stderr_tail = stderr_fh.read()[-2000:]
    except OSError:
        stderr_tail = ""
    finally:
        stderr_fh.close()
    log_exit(proc, purpose="hls-remux", stderr_tail=redact_text(stderr_tail))

    if cancelled:
        # The partial output is intermediate work in the caller's workspace
        # — the caller (core.downloader) owns cleaning it up as part of the
        # batch's normal cancel handling; this function only reports that
        # the remux did not complete.
        raise HlsCancelled(f"ffmpeg cancelled for {out.name}")
    if timed_out:
        raise RuntimeError(f"ffmpeg timed out after {timeout_sec}s")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited {proc.returncode}:\n{redact_text(stderr_tail)}")

    elapsed = time.monotonic() - start
    size    = out.stat().st_size if out.exists() else 0
    logger.info(
        "hls_downloader: finished %s → %.1f MB in %.1fs",
        out.name, size / 1_048_576, elapsed,
    )
    return str(out)


def _netscape_to_cookie_header(path: str) -> str:
    """
    Parse a Netscape cookies.txt and return a single `Cookie: k=v; ...` value.
    Lines starting with # are skipped.  Only unexpired cookies are included.
    """
    from utils.cookie_store import read_cookie_store
    try:
        return _netscape_text_to_cookie_header(read_cookie_store(path))
    except OSError:
        logger.debug("_netscape_to_cookie_header: cookie store could not be read")
        return ""


def _netscape_text_to_cookie_header(text: str) -> str:
    """Convert Netscape text already held in private memory to one header."""
    now = time.time()
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        _domain, _flag, _path, _secure, expires_str, name, value = (
            fields[0], fields[1], fields[2], fields[3],
            fields[4], fields[5], fields[6],
        )
        try:
            expires = float(expires_str)
            if expires > 0 and expires < now:
                continue
        except (ValueError, TypeError):
            pass
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def probe_stream(url: str, timeout_sec: int = 10) -> dict:
    """
    Use ffprobe to get basic stream info (duration, codec, bitrate).
    Returns {} if ffprobe is unavailable or the URL fails.
    """
    ffprobe = _find_ffprobe()
    cmd = [
        ffprobe, "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        url,
    ]
    result = run_hidden(cmd, purpose="hls-probe", timeout=timeout_sec)
    if result.returncode == 0:
        try:
            import json
            return json.loads(result.stdout)
        except ValueError:
            logger.warning("hls_downloader: ffprobe returned unparsable JSON")
    return {}
