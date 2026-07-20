"""
core/converter_service.py  –  Hardened local audio conversion service
======================================================================
Phase 3 (converter release hardening) core layer.  All conversion logic
lives here, outside any QWidget, so it can be unit- and integration-
tested without a GUI and reused by the CLI smoke mode.

Guarantees
----------
* No silent overwrite: an explicit :class:`CollisionPolicy` governs every
  existing-destination case, and the source file is never overwritten.
* Atomic output: FFmpeg writes into a unique temp file in the destination
  directory; the temp is verified with FFprobe (stream, codec, duration)
  and only then moved into place with ``os.replace``.  On any failure the
  temp is removed and the destination is left untouched.
* Real cancellation: FFmpeg runs under ``subprocess.Popen`` with a
  process handle registered on the :class:`CancellationToken`; cancel
  performs a graceful stop, then a forced kill, and reports a
  ``CANCELLED`` outcome distinct from errors.
* Real progress: parsed from FFmpeg ``-progress pipe:1`` against the
  FFprobe duration of the source.  No fabricated percentages: when the
  duration is unknown the ratio is ``None`` (indeterminate).

Zero GUI imports.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from utils.paths import get_ffmpeg_executable, get_ffprobe_executable
from utils.security import redact_text

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────────────


class CollisionPolicy(str, Enum):
    """What to do when the destination file already exists."""

    ASK = "ask"                # caller must resolve via ask_callback / pre-scan
    SKIP = "skip"
    UNIQUE = "unique"          # generate "name (2).ext"
    OVERWRITE = "overwrite"    # explicit, atomic replace — never the source


class FileOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ConversionErrorKind(str, Enum):
    NONE = "none"
    FFMPEG_MISSING = "ffmpeg_missing"
    FFPROBE_MISSING = "ffprobe_missing"
    SOURCE_MISSING = "source_missing"
    CORRUPT_SOURCE = "corrupt_source"
    UNSUPPORTED_CODEC = "unsupported_codec"
    UNSUPPORTED_CONTAINER = "unsupported_container"
    NO_AUDIO_STREAM = "no_audio_stream"
    PERMISSION_DENIED = "permission_denied"
    DESTINATION_LOCKED = "destination_locked"
    SOURCE_LOCKED = "source_locked"
    DISK_FULL = "disk_full"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_OUTPUT = "invalid_output"
    SAME_FILE = "same_file"
    COLLISION_UNRESOLVED = "collision_unresolved"
    PATH_TOO_LONG = "path_too_long"
    UNKNOWN = "unknown"


#: Formats whose containers cannot hold the full tag/artwork set.  The UI
#: must warn before converting into one of these.
LOSSY_METADATA_TARGETS: dict[str, tuple[str, ...]] = {
    "wav": ("tags", "artwork"),
}

_EXPECTED_AUDIO_CODEC = {
    "mp3": ("mp3",),
    "m4a": ("aac", "alac"),
    "flac": ("flac",),
    "opus": ("opus",),
    "wav": ("pcm_s16le", "pcm_s24le", "pcm_f32le"),
}

_EXPECTED_CONTAINER_TOKENS = {
    "mp3": ("mp3",),
    "m4a": ("mp4", "mov", "m4a"),
    "flac": ("flac",),
    "opus": ("ogg", "opus"),
    "wav": ("wav",),
}


@dataclass(frozen=True)
class ConversionRequest:
    """One source file plus every knob the conversion needs."""

    source: Path
    output_format: str                       # mp3 / m4a / flac / opus / wav
    bitrate: Optional[str] = None            # e.g. "320k"; lossy targets only
    output_dir: Optional[Path] = None        # default: source's own folder
    collision_policy: CollisionPolicy = CollisionPolicy.ASK
    preserve_metadata: bool = True
    preserve_artwork: bool = True
    timeout_seconds: float = 1800.0


@dataclass
class FileProgress:
    source: str
    seconds_done: float
    total_seconds: Optional[float]
    ratio: Optional[float]                   # 0.0–1.0, None = indeterminate
    stage: str = "converting"                # probing / converting / verifying / finalizing


@dataclass
class FileResult:
    source: str
    destination: Optional[str]
    outcome: FileOutcome
    error_kind: ConversionErrorKind = ConversionErrorKind.NONE
    message: str = ""
    metadata_preserved: bool = True
    artwork_preserved: bool = True
    warnings: tuple[str, ...] = ()


@dataclass
class BatchProgress:
    index: int                               # 1-based position of current file
    total: int
    current_source: str
    completed: int
    failed: int
    skipped: int
    cancelled: int
    current_ratio: Optional[float]
    batch_ratio: Optional[float]
    eta_seconds: Optional[float]


@dataclass
class BatchResult:
    results: list[FileResult] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return sum(1 for r in self.results if r.outcome is FileOutcome.COMPLETED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome is FileOutcome.FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.outcome is FileOutcome.SKIPPED)

    @property
    def cancelled(self) -> int:
        return sum(1 for r in self.results if r.outcome is FileOutcome.CANCELLED)


class CancellationToken:
    """Thread-safe cancel flag that also stops the live FFmpeg process."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._process: Optional[subprocess.Popen] = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            proc = self._process
        if proc is not None:
            _stop_process(proc)

    def _register(self, proc: Optional[subprocess.Popen]) -> None:
        with self._lock:
            self._process = proc
        # A cancel that raced the registration must still stop the process.
        if proc is not None and self._event.is_set():
            _stop_process(proc)


def _stop_process(proc: subprocess.Popen, grace_seconds: float = 3.0) -> None:
    """Graceful stop, then forced kill; never leaves an orphan behind."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            # CTRL_BREAK reaches the whole process group created with
            # CREATE_NEW_PROCESS_GROUP and lets FFmpeg shut down cleanly.
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=grace_seconds)
    except (OSError, subprocess.TimeoutExpired):
        logger.error("[Converter] FFmpeg process %s did not exit after kill", proc.pid)


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────


def _popen_kwargs() -> dict:
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _same_file(a: Path, b: Path) -> bool:
    """True when both paths refer to the same physical file.

    Handles case-only differences on case-insensitive filesystems and
    symlinks/junctions via samefile when both ends exist.
    """
    try:
        if b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))


def _unique_destination(dest: Path, source: Path) -> Path:
    stem, suffix = dest.stem, dest.suffix
    for n in range(2, 1000):
        candidate = dest.with_name(f"{stem} ({n}){suffix}")
        if not candidate.exists() and not _same_file(source, candidate):
            return candidate
    raise FileExistsError(f"no free unique name for {dest.name}")


def _classify_oserror(exc: OSError) -> ConversionErrorKind:
    if isinstance(exc, FileNotFoundError):
        return ConversionErrorKind.SOURCE_MISSING
    winerr = getattr(exc, "winerror", None)
    if winerr in (32, 33):                       # sharing violation / lock
        return ConversionErrorKind.DESTINATION_LOCKED
    if winerr == 206 or exc.errno == errno.ENAMETOOLONG:
        return ConversionErrorKind.PATH_TOO_LONG
    if exc.errno == errno.ENOSPC:
        return ConversionErrorKind.DISK_FULL
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return ConversionErrorKind.PERMISSION_DENIED
    return ConversionErrorKind.UNKNOWN


def _classify_ffmpeg_stderr(stderr_text: str) -> ConversionErrorKind:
    text = stderr_text.lower()
    if "matches no streams" in text or "does not contain any stream" in text:
        return ConversionErrorKind.NO_AUDIO_STREAM
    if "invalid data found" in text or "could not find codec parameters" in text:
        return ConversionErrorKind.CORRUPT_SOURCE
    if "unknown encoder" in text or "encoder not found" in text:
        return ConversionErrorKind.UNSUPPORTED_CODEC
    if "unable to find a suitable output format" in text or "muxer not found" in text:
        return ConversionErrorKind.UNSUPPORTED_CONTAINER
    if "permission denied" in text:
        return ConversionErrorKind.PERMISSION_DENIED
    if "no space left" in text:
        return ConversionErrorKind.DISK_FULL
    return ConversionErrorKind.UNKNOWN


_PROGRESS_TIME_RE = re.compile(r"^out_time_(us|ms)=(-?\d+)")


# ──────────────────────────────────────────────────────────────────────────────
# ConverterService
# ──────────────────────────────────────────────────────────────────────────────


class ConverterService:
    """Stateless conversion engine; one instance can serve many requests."""

    def __init__(
        self,
        ffmpeg_path: Optional[str] = None,
        ffprobe_path: Optional[str] = None,
    ) -> None:
        self._ffmpeg = ffmpeg_path or get_ffmpeg_executable()
        self._ffprobe = ffprobe_path or get_ffprobe_executable()

    # ── probing ────────────────────────────────────────────────────────────────

    def probe(self, path: Path, timeout: float = 60.0) -> dict:
        """Return FFprobe JSON for *path*; raises on failure."""
        if not self._ffprobe:
            raise FileNotFoundError("ffprobe executable not found")
        cmd = [
            self._ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, **(
                {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            ),
        )
        if proc.returncode != 0:
            raise ValueError(proc.stderr.decode(errors="replace")[-400:] or "ffprobe failed")
        return json.loads(proc.stdout.decode(errors="replace") or "{}")

    @staticmethod
    def _audio_stream(probe_data: dict) -> Optional[dict]:
        for stream in probe_data.get("streams", ()):
            if stream.get("codec_type") == "audio":
                return stream
        return None

    @staticmethod
    def _duration_of(probe_data: dict) -> Optional[float]:
        raw = (probe_data.get("format") or {}).get("duration")
        if raw is None:
            stream = ConverterService._audio_stream(probe_data) or {}
            raw = stream.get("duration")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # ── destination resolution ─────────────────────────────────────────────────

    def resolve_destination(
        self,
        request: ConversionRequest,
        ask_callback: Optional[Callable[[Path, Path], CollisionPolicy]] = None,
    ) -> tuple[Optional[Path], Optional[FileResult]]:
        """Compute the output path under the request's collision policy.

        Returns ``(destination, None)`` to proceed or ``(None, FileResult)``
        when the file must be skipped or failed without converting.
        """
        src = Path(request.source)
        dest_dir = Path(request.output_dir) if request.output_dir else src.parent
        # src.name is a bare filename, so the joined path cannot traverse
        # outside dest_dir.
        dest = dest_dir / f"{src.stem}.{request.output_format}"

        policy = request.collision_policy

        if _same_file(src, dest):
            # Converting into the source's own name (same format + folder,
            # possibly differing only by case).  Never overwrite the source —
            # fall through to a unique sibling regardless of policy.
            try:
                dest = _unique_destination(dest, src)
            except FileExistsError as exc:
                return None, FileResult(
                    source=str(src), destination=None, outcome=FileOutcome.FAILED,
                    error_kind=ConversionErrorKind.SAME_FILE, message=str(exc),
                )
            return dest, None

        if dest.exists():
            if policy is CollisionPolicy.ASK:
                if ask_callback is None:
                    return None, FileResult(
                        source=str(src), destination=str(dest),
                        outcome=FileOutcome.SKIPPED,
                        error_kind=ConversionErrorKind.COLLISION_UNRESOLVED,
                        message="destination exists and no collision decision was provided",
                    )
                policy = ask_callback(src, dest)
            if policy is CollisionPolicy.SKIP or policy is CollisionPolicy.ASK:
                return None, FileResult(
                    source=str(src), destination=str(dest),
                    outcome=FileOutcome.SKIPPED,
                    message="destination exists",
                )
            if policy is CollisionPolicy.UNIQUE:
                try:
                    dest = _unique_destination(dest, src)
                except FileExistsError as exc:
                    return None, FileResult(
                        source=str(src), destination=None,
                        outcome=FileOutcome.FAILED,
                        error_kind=ConversionErrorKind.UNKNOWN, message=str(exc),
                    )
            # OVERWRITE: keep dest; the atomic os.replace at the end
            # replaces it only after the new file fully verified.
        return dest, None

    # ── single file ────────────────────────────────────────────────────────────

    def convert_file(
        self,
        request: ConversionRequest,
        on_progress: Optional[Callable[[FileProgress], None]] = None,
        cancel: Optional[CancellationToken] = None,
        ask_callback: Optional[Callable[[Path, Path], CollisionPolicy]] = None,
    ) -> FileResult:
        src = Path(request.source)
        cancel = cancel or CancellationToken()

        def _progress(stage: str, done: float, total: Optional[float]) -> None:
            if on_progress is None:
                return
            ratio = None
            if total and total > 0:
                ratio = max(0.0, min(1.0, done / total))
            on_progress(FileProgress(
                source=str(src), seconds_done=done, total_seconds=total,
                ratio=ratio, stage=stage,
            ))

        if cancel.cancelled:
            return self._cancelled_result(src)
        if not self._ffmpeg:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.FFMPEG_MISSING,
                message="FFmpeg executable not found",
            )
        if not self._ffprobe:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.FFPROBE_MISSING,
                message="FFprobe executable not found",
            )
        if not src.is_file():
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.SOURCE_MISSING,
                message="source file not found",
            )

        # 1. Probe the source: validates readability, finds the audio
        #    stream, and yields the duration that anchors real progress.
        _progress("probing", 0.0, None)
        try:
            probe_data = self.probe(src)
        except subprocess.TimeoutExpired:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.TIMEOUT, message="ffprobe timed out",
            )
        except FileNotFoundError:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.FFPROBE_MISSING,
                message="FFprobe executable not found",
            )
        except PermissionError as exc:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.SOURCE_LOCKED,
                message=redact_text(str(exc)),
            )
        except (ValueError, json.JSONDecodeError, OSError) as exc:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.CORRUPT_SOURCE,
                message=redact_text(str(exc)),
            )
        if self._audio_stream(probe_data) is None:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.NO_AUDIO_STREAM,
                message="source has no audio stream",
            )
        duration = self._duration_of(probe_data)

        # 2. Resolve destination under the collision policy.
        dest, early = self.resolve_destination(request, ask_callback)
        if early is not None:
            return early
        assert dest is not None
        dest_dir = dest.parent

        # 3. Preflight: directory, writability, disk space.
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return FileResult(
                source=str(src), destination=str(dest), outcome=FileOutcome.FAILED,
                error_kind=_classify_oserror(exc), message=redact_text(str(exc)),
            )
        try:
            free = shutil.disk_usage(dest_dir).free
            needed = int(src.stat().st_size * 1.5) + 16 * 1024 * 1024
            if free < needed:
                return FileResult(
                    source=str(src), destination=str(dest),
                    outcome=FileOutcome.FAILED,
                    error_kind=ConversionErrorKind.DISK_FULL,
                    message=f"free space {free} below required estimate {needed}",
                )
        except OSError:
            pass  # disk_usage may fail on exotic mounts; FFmpeg will surface ENOSPC

        temp = dest_dir / f".{dest.stem}.{uuid.uuid4().hex[:8]}.part.{request.output_format}"

        # 4. Run FFmpeg into the temp file.
        result = self._run_ffmpeg(request, src, temp, duration, cancel, _progress)
        if result is not None:
            self._cleanup_temp(temp)
            return result

        # 5. Verify the temp output before it may replace anything.
        _progress("verifying", duration or 0.0, duration)
        verify_error = self._verify_output(request, temp, duration)
        if verify_error is not None:
            self._cleanup_temp(temp)
            return FileResult(
                source=str(src), destination=str(dest), outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.INVALID_OUTPUT, message=verify_error,
            )
        if cancel.cancelled:
            self._cleanup_temp(temp)
            return self._cancelled_result(src)

        # 6. Artwork/metadata preservation into the verified temp.
        warnings: list[str] = []
        metadata_ok = True
        artwork_ok = True
        fmt_losses = LOSSY_METADATA_TARGETS.get(request.output_format, ())
        if "tags" in fmt_losses:
            metadata_ok = False
            warnings.append("target format cannot store the full tag set")
        if request.preserve_artwork:
            if "artwork" in fmt_losses:
                artwork_ok = False
                warnings.append("target format cannot store embedded artwork")
            else:
                copied = self._copy_artwork(src, temp, request.output_format)
                if copied is False:
                    artwork_ok = False
                    warnings.append("embedded artwork could not be copied")

        # 7. Atomic replace.
        _progress("finalizing", duration or 0.0, duration)
        try:
            if dest.exists() and request.collision_policy is not CollisionPolicy.OVERWRITE:
                # The destination appeared between resolution and now.
                if request.collision_policy is CollisionPolicy.UNIQUE:
                    dest = _unique_destination(dest, src)
                else:
                    self._cleanup_temp(temp)
                    return FileResult(
                        source=str(src), destination=str(dest),
                        outcome=FileOutcome.SKIPPED,
                        error_kind=ConversionErrorKind.COLLISION_UNRESOLVED,
                        message="destination appeared during conversion",
                    )
            if _same_file(src, dest):
                self._cleanup_temp(temp)
                return FileResult(
                    source=str(src), destination=str(dest),
                    outcome=FileOutcome.FAILED,
                    error_kind=ConversionErrorKind.SAME_FILE,
                    message="refusing to replace the source file",
                )
            os.replace(temp, dest)
        except OSError as exc:
            self._cleanup_temp(temp)
            return FileResult(
                source=str(src), destination=str(dest), outcome=FileOutcome.FAILED,
                error_kind=_classify_oserror(exc), message=redact_text(str(exc)),
            )

        _progress("finalizing", duration or 1.0, duration or 1.0)
        return FileResult(
            source=str(src), destination=str(dest), outcome=FileOutcome.COMPLETED,
            metadata_preserved=metadata_ok, artwork_preserved=artwork_ok,
            warnings=tuple(warnings),
        )

    # ── batch ──────────────────────────────────────────────────────────────────

    def convert_batch(
        self,
        requests: list[ConversionRequest],
        on_batch_progress: Optional[Callable[[BatchProgress], None]] = None,
        on_file_result: Optional[Callable[[FileResult], None]] = None,
        cancel: Optional[CancellationToken] = None,
        ask_callback: Optional[Callable[[Path, Path], CollisionPolicy]] = None,
    ) -> BatchResult:
        cancel = cancel or CancellationToken()
        batch = BatchResult()
        total = len(requests)
        started = time.monotonic()

        for index, request in enumerate(requests, start=1):
            if cancel.cancelled:
                batch.results.append(self._cancelled_result(Path(request.source)))
                if on_file_result:
                    on_file_result(batch.results[-1])
                continue

            def _file_progress(fp: FileProgress, _index: int = index) -> None:
                if on_batch_progress is None:
                    return
                done_files = _index - 1
                batch_ratio = None
                eta = None
                if total > 0:
                    within = fp.ratio if fp.ratio is not None else 0.0
                    batch_ratio = (done_files + within) / total
                    elapsed = time.monotonic() - started
                    if batch_ratio > 0.02:
                        eta = max(0.0, elapsed / batch_ratio - elapsed)
                on_batch_progress(BatchProgress(
                    index=_index, total=total, current_source=fp.source,
                    completed=batch.completed, failed=batch.failed,
                    skipped=batch.skipped, cancelled=batch.cancelled,
                    current_ratio=fp.ratio, batch_ratio=batch_ratio,
                    eta_seconds=eta,
                ))

            result = self.convert_file(
                request, on_progress=_file_progress, cancel=cancel,
                ask_callback=ask_callback,
            )
            batch.results.append(result)
            if on_file_result:
                on_file_result(result)
        return batch

    # ── internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _cancelled_result(src: Path) -> FileResult:
        return FileResult(
            source=str(src), destination=None, outcome=FileOutcome.CANCELLED,
            error_kind=ConversionErrorKind.CANCELLED, message="cancelled by user",
        )

    def _build_command(
        self, request: ConversionRequest, src: Path, temp: Path,
    ) -> list[str]:
        cmd = [
            self._ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
            "-i", str(src),
            "-map", "0:a:0", "-vn",
        ]
        if request.preserve_metadata:
            cmd += ["-map_metadata", "0"]
        else:
            cmd += ["-map_metadata", "-1"]
        fmt = request.output_format
        if fmt in ("mp3", "m4a", "opus") and request.bitrate:
            cmd += ["-b:a", request.bitrate]
        if fmt == "mp3":
            cmd += ["-codec:a", "libmp3lame", "-f", "mp3"]
        elif fmt == "m4a":
            cmd += ["-codec:a", "aac", "-movflags", "+faststart", "-f", "mp4"]
        elif fmt == "flac":
            cmd += ["-codec:a", "flac", "-compression_level", "8", "-f", "flac"]
        elif fmt == "opus":
            cmd += ["-codec:a", "libopus", "-f", "ogg"]
        elif fmt == "wav":
            cmd += ["-codec:a", "pcm_s16le", "-f", "wav"]
        else:
            raise ValueError(f"unsupported output format: {fmt}")
        cmd += ["-progress", "pipe:1"]
        # "-y" applies only to the fresh uuid temp file, never to the real
        # destination; without it FFmpeg would block on a confirm prompt if
        # the temp name ever collided.
        cmd += ["-y", str(temp)]
        return cmd

    def _run_ffmpeg(
        self,
        request: ConversionRequest,
        src: Path,
        temp: Path,
        duration: Optional[float],
        cancel: CancellationToken,
        progress: Callable[[str, float, Optional[float]], None],
    ) -> Optional[FileResult]:
        """Run the conversion; returns a FileResult on failure/cancel, else None."""
        try:
            cmd = self._build_command(request, src, temp)
        except ValueError as exc:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.UNSUPPORTED_CONTAINER, message=str(exc),
            )
        try:
            proc = subprocess.Popen(cmd, **_popen_kwargs())
        except FileNotFoundError:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.FFMPEG_MISSING,
                message="FFmpeg executable not found",
            )
        except OSError as exc:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=_classify_oserror(exc), message=redact_text(str(exc)),
            )

        cancel._register(proc)
        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # The stdout loop blocks on the pipe, so a stalled FFmpeg that emits
        # nothing would starve an in-loop deadline check.  A watchdog timer
        # enforces the timeout out-of-band by stopping the process, which
        # closes the pipe and unblocks the loop.
        timed_out_event = threading.Event()

        def _on_timeout() -> None:
            timed_out_event.set()
            _stop_process(proc)

        watchdog = threading.Timer(max(request.timeout_seconds, 1.0), _on_timeout)
        watchdog.daemon = True
        watchdog.start()
        try:
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    line = raw_line.decode(errors="replace").strip()
                    match = _PROGRESS_TIME_RE.match(line)
                    if match:
                        unit, value = match.group(1), int(match.group(2))
                        seconds = value / 1_000_000 if unit == "us" else value / 1000
                        # FFmpeg's out_time_ms field is historically also
                        # microseconds; normalize impossible values.
                        if unit == "ms" and duration and seconds > duration * 10:
                            seconds = value / 1_000_000
                        progress("converting", max(0.0, seconds), duration)
            # The watchdog (timeout) and cancel (user) both stop the process,
            # so this wait only bridges the gap between pipe close and exit.
            proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            _stop_process(proc)
        finally:
            watchdog.cancel()
            cancel._register(None)
            stderr_thread.join(timeout=5.0)
            if proc.poll() is None:
                _stop_process(proc)

        timed_out = timed_out_event.is_set()
        if cancel.cancelled:
            return self._cancelled_result(src)
        if timed_out:
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=ConversionErrorKind.TIMEOUT,
                message=f"conversion exceeded {int(request.timeout_seconds)}s",
            )
        if proc.returncode != 0:
            stderr_text = b"".join(stderr_chunks).decode(errors="replace")
            return FileResult(
                source=str(src), destination=None, outcome=FileOutcome.FAILED,
                error_kind=_classify_ffmpeg_stderr(stderr_text),
                message=redact_text(stderr_text[-400:]) or "FFmpeg error",
            )
        return None

    def _verify_output(
        self,
        request: ConversionRequest,
        temp: Path,
        source_duration: Optional[float],
    ) -> Optional[str]:
        """Return an error message when the temp output fails verification."""
        try:
            if not temp.is_file() or temp.stat().st_size == 0:
                return "output file is missing or empty"
        except OSError as exc:
            return redact_text(str(exc))
        try:
            probe_data = self.probe(temp)
        except Exception as exc:  # noqa: BLE001 — any probe failure fails closed
            return f"output not probeable: {redact_text(str(exc))[:200]}"

        stream = self._audio_stream(probe_data)
        if stream is None:
            return "output has no audio stream"

        codec = str(stream.get("codec_name", "")).lower()
        expected_codecs = _EXPECTED_AUDIO_CODEC.get(request.output_format, ())
        if expected_codecs and codec not in expected_codecs:
            return f"output codec {codec!r} does not match {request.output_format}"

        container = str((probe_data.get("format") or {}).get("format_name", "")).lower()
        tokens = _EXPECTED_CONTAINER_TOKENS.get(request.output_format, ())
        if tokens and not any(tok in container for tok in tokens):
            return f"output container {container!r} does not match {request.output_format}"

        out_duration = self._duration_of(probe_data)
        if source_duration and out_duration:
            tolerance = max(2.0, source_duration * 0.05)
            if abs(out_duration - source_duration) > tolerance:
                return (
                    f"output duration {out_duration:.2f}s deviates from "
                    f"source {source_duration:.2f}s beyond {tolerance:.2f}s"
                )
        return None

    @staticmethod
    def _cleanup_temp(temp: Path) -> None:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            logger.warning("[Converter] could not remove temp file %s", temp.name)

    # ── artwork ────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_source_artwork(src: Path) -> Optional[tuple[bytes, str]]:
        """Return (image_bytes, mime) from the source file, if any."""
        try:
            import mutagen
            from mutagen.flac import FLAC
            from mutagen.id3 import ID3
            from mutagen.mp4 import MP4
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis
        except ImportError:
            return None
        suffix = src.suffix.lower()
        try:
            if suffix == ".mp3":
                tags = ID3(str(src))
                pics = tags.getall("APIC")
                if pics:
                    return bytes(pics[0].data), pics[0].mime or "image/jpeg"
            elif suffix in (".m4a", ".mp4"):
                mp4 = MP4(str(src))
                covers = mp4.tags.get("covr") if mp4.tags else None
                if covers:
                    cover = covers[0]
                    mime = "image/png" if getattr(cover, "imageformat", 13) == 14 else "image/jpeg"
                    return bytes(cover), mime
            elif suffix == ".flac":
                flac = FLAC(str(src))
                if flac.pictures:
                    pic = flac.pictures[0]
                    return bytes(pic.data), pic.mime or "image/jpeg"
            elif suffix in (".ogg", ".opus"):
                import base64
                from mutagen.flac import Picture
                audio = OggOpus(str(src)) if suffix == ".opus" else OggVorbis(str(src))
                blocks = audio.get("metadata_block_picture", [])
                if blocks:
                    pic = Picture(base64.b64decode(blocks[0]))
                    return bytes(pic.data), pic.mime or "image/jpeg"
            else:
                generic = mutagen.File(str(src))
                pictures = getattr(generic, "pictures", None)
                if pictures:
                    return bytes(pictures[0].data), pictures[0].mime or "image/jpeg"
        except Exception:  # noqa: BLE001 — artwork is best-effort, never fatal
            logger.debug("[Converter] artwork read failed for %s", src.name, exc_info=True)
        return None

    @classmethod
    def _copy_artwork(cls, src: Path, target: Path, output_format: str) -> Optional[bool]:
        """Copy embedded artwork from src into target.

        Returns True on success, None when the source has no artwork, and
        False when the source has artwork that could not be written.
        """
        art = cls._read_source_artwork(src)
        if art is None:
            return None
        data, mime = art
        try:
            if output_format == "mp3":
                from mutagen.id3 import APIC, ID3, ID3NoHeaderError
                try:
                    tags = ID3(str(target))
                except ID3NoHeaderError:
                    tags = ID3()
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                tags.save(str(target))
            elif output_format == "m4a":
                from mutagen.mp4 import MP4, MP4Cover
                fmt = (
                    MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                )
                mp4 = MP4(str(target))
                if mp4.tags is None:
                    mp4.add_tags()
                mp4.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
                mp4.save()
            elif output_format == "flac":
                from mutagen.flac import FLAC, Picture
                flac = FLAC(str(target))
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover"
                pic.data = data
                flac.clear_pictures()
                flac.add_picture(pic)
                flac.save()
            elif output_format == "opus":
                import base64
                from mutagen.flac import Picture
                from mutagen.oggopus import OggOpus
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.desc = "Cover"
                pic.data = data
                opus = OggOpus(str(target))
                opus["metadata_block_picture"] = [
                    base64.b64encode(pic.write()).decode("ascii")
                ]
                opus.save()
            else:
                return False
        except Exception:  # noqa: BLE001 — artwork failure must not kill the file
            logger.debug(
                "[Converter] artwork write failed for %s", target.name, exc_info=True,
            )
            return False
        return True


def format_preservation_warnings(output_format: str) -> tuple[str, ...]:
    """What the given target format will lose; empty when nothing is lost."""
    return LOSSY_METADATA_TARGETS.get(output_format, ())
