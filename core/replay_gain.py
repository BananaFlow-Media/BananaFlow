"""
core/replay_gain.py  –  ReplayGain analysis + tag embedding (Advanced Setting)
===============================================================================
Analyses downloaded audio files for loudness and embeds ReplayGain tags
so every track plays at a normalised volume in music players.

Uses the ``rsgain`` CLI tool if available (most accurate, fastest) and falls
back to the pure-Python ``pyloudnorm`` + ``soundfile`` stack when rsgain is
not installed.

Supported containers (via mutagen)
-----------------------------------
  MP3   → ID3  REPLAYGAIN_TRACK_GAIN / REPLAYGAIN_TRACK_PEAK
  FLAC  → Vorbis comment REPLAYGAIN_TRACK_GAIN / REPLAYGAIN_TRACK_PEAK
  M4A   → iTunes atom  com.apple.iTunes REPLAYGAIN_TRACK_GAIN
  OGG   → Vorbis comment (same as FLAC)

Reference loudness: –18 LUFS (EBU R128 / ReplayGain 2.0 standard).

This module is **disabled by default** and only called when
AppConfig.replay_gain_enabled is True.

Zero GUI imports.  All errors are logged; never raises to the caller.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from core.metadata_models import (
    AudioTrackItem,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_REFERENCE_LOUDNESS,
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
)

logger = logging.getLogger(__name__)

# EBU R128 analysis target used by ReplayGain 2.0. The optional stored
# REPLAYGAIN_REFERENCE_LOUDNESS tag uses the ReplayGain playback convention
# (89 dB), not this digital-domain LUFS value.
_REFERENCE_LUFS: float = -18.0
_REFERENCE_LOUDNESS_DB: float = 89.0


class ReplayGainAnalysisError(RuntimeError):
    """Actionable analysis failure; no metadata or audio was modified."""


class ReplayGainAnalysisCancelled(ReplayGainAnalysisError):
    pass


@dataclass(frozen=True)
class ReplayGainAnalysis:
    path: Path
    track_gain_db: float
    track_peak: float
    loudness_lufs: float
    duration_seconds: float
    album_gain_db: float | None = None
    album_peak: float | None = None

    def proposal_values(self, *, include_album: bool = False) -> dict[str, float]:
        values = {
            REPLAYGAIN_TRACK_GAIN: self.track_gain_db,
            REPLAYGAIN_TRACK_PEAK: self.track_peak,
            REPLAYGAIN_REFERENCE_LOUDNESS: _REFERENCE_LOUDNESS_DB,
        }
        if include_album and self.album_gain_db is not None and self.album_peak is not None:
            values[REPLAYGAIN_ALBUM_GAIN] = self.album_gain_db
            values[REPLAYGAIN_ALBUM_PEAK] = self.album_peak
        return values


@dataclass(frozen=True)
class AlbumGroup:
    key: tuple[str, str, str]
    tracks: tuple[AudioTrackItem, ...]
    ambiguous: bool = False


def group_album_scope(
    tracks: Iterable[AudioTrackItem],
    *,
    item_id: Callable[[AudioTrackItem], object] | None = None,
) -> tuple[AlbumGroup, ...]:
    """Conservatively group a selection by effective canonical album identity.

    Album artist (falling back to artist), album and meaningful year/date form
    a safe identity. Missing album *or* artist identity is ambiguous and never
    silently merged. Duplicate references to one workspace item are ignored;
    filenames, paths and selection order never manufacture album identity.
    """
    identity_of = item_id or id
    grouped: dict[tuple[str, str, str], list[AudioTrackItem]] = {}
    ambiguous: list[AlbumGroup] = []
    seen: set[object] = set()
    for track in tracks:
        identity = identity_of(track)
        if identity in seen:
            continue
        seen.add(identity)
        tags = track.proposed.effective_tags(track.original)
        album = tags.album.strip().casefold()
        artist = (tags.album_artist or tags.artist).strip().casefold()
        year = tags.year.strip().casefold()
        if not album or not artist:
            ambiguous.append(AlbumGroup((f"#{identity}", album, year), (track,), True))
            continue
        grouped.setdefault((artist, album, year), []).append(track)

    def track_order(track: AudioTrackItem) -> tuple[int, int, str]:
        tags = track.proposed.effective_tags(track.original)
        return (
            tags.disc_num if tags.disc_num is not None else 2**31 - 1,
            tags.track_num if tags.track_num is not None else 2**31 - 1,
            str(identity_of(track)),
        )

    groups = [
        AlbumGroup(key, tuple(sorted(items, key=track_order)))
        for key, items in sorted(grouped.items())
    ]
    groups.extend(sorted(ambiguous, key=lambda group: group.key))
    return tuple(groups)


def analyse_track(
    file_path: str | Path,
    *,
    cancel_event: threading.Event | None = None,
) -> ReplayGainAnalysis:
    """Analyze one track without writing tags or changing audio bytes."""
    path = Path(file_path)
    if cancel_event is not None and cancel_event.is_set():
        raise ReplayGainAnalysisCancelled("cancelled")
    if not path.exists():
        raise ReplayGainAnalysisError(f"file not found: {path}")
    if not path.is_file():
        raise ReplayGainAnalysisError(f"not a file: {path}")
    try:
        file_size = path.stat().st_size
        # Keep filesystem failures distinct from decoder capability failures.
        with path.open("rb"):
            pass
    except OSError as exc:
        raise ReplayGainAnalysisError(f"could not read {path}: {exc}") from exc

    if file_size > _MAX_FILE_SIZE_BYTES:
        # The Python decoder reads the whole stream; FFmpeg streams it and is
        # therefore the safe boundary for large files.
        return _analyse_track_with_ffmpeg(path, cancel_event=cancel_event)

    try:
        stack = _python_analysis_stack()
    except (ImportError, OSError):
        return _analyse_track_with_ffmpeg(path, cancel_event=cancel_event)

    try:
        return _analyse_track_with_python(path, stack, cancel_event=cancel_event)
    except ReplayGainAnalysisCancelled:
        raise
    except ReplayGainAnalysisError as python_error:
        if cancel_event is not None and cancel_event.is_set():
            raise ReplayGainAnalysisCancelled("cancelled") from python_error
        try:
            return _analyse_track_with_ffmpeg(path, cancel_event=cancel_event)
        except ReplayGainAnalysisCancelled:
            raise
        except ReplayGainAnalysisError as ffmpeg_error:
            raise ReplayGainAnalysisError(
                f"both decoders failed for {path.name}; "
                f"Python: {python_error}; FFmpeg: {ffmpeg_error}"
            ) from ffmpeg_error


def _python_analysis_stack():
    import soundfile as sf  # type: ignore[import]
    import pyloudnorm as pyln  # type: ignore[import]
    import numpy as np
    return sf, pyln, np


def _analyse_track_with_python(
    path: Path,
    stack,
    *,
    cancel_event: threading.Event | None = None,
) -> ReplayGainAnalysis:
    sf, pyln, np = stack

    with _analysis_lock:
        if cancel_event is not None and cancel_event.is_set():
            raise ReplayGainAnalysisCancelled("cancelled")
        try:
            data, rate = sf.read(str(path), always_2d=True)
        except Exception as exc:
            raise ReplayGainAnalysisError(f"could not decode {path.name}: {exc}") from exc
        if not isinstance(rate, (int, float)) or rate <= 0 or data is None:
            raise ReplayGainAnalysisError(f"decoder returned malformed audio for {path.name}")
        if cancel_event is not None and cancel_event.is_set():
            del data
            raise ReplayGainAnalysisCancelled("cancelled")
        try:
            loudness = float(pyln.Meter(rate).integrated_loudness(data))
            peak = float(np.abs(data).max()) if getattr(data, "size", 0) else 0.0
            duration = float(len(data)) / float(rate)
        except Exception as exc:
            raise ReplayGainAnalysisError(f"analysis failed for {path.name}: {exc}") from exc
        finally:
            del data
    if loudness == -math.inf and peak == 0.0:
        # FFmpeg's ebur128 summary represents digital silence at its -70 LUFS
        # floor. Keep both analyzers consistent and the proposal finite.
        loudness = -70.0
    if not math.isfinite(loudness) or not math.isfinite(peak):
        raise ReplayGainAnalysisError(f"analyzer returned non-finite values for {path.name}")
    return ReplayGainAnalysis(
        path=path,
        track_gain_db=_REFERENCE_LUFS - loudness,
        track_peak=peak,
        loudness_lufs=loudness,
        duration_seconds=duration,
    )


_FFMPEG_LOUDNESS_RE = re.compile(r"\bI:\s*(-?(?:\d+(?:\.\d*)?|\.\d+))\s+LUFS", re.IGNORECASE)
_FFMPEG_PEAK_RE = re.compile(
    r"\bPeak:\s*(-?(?:(?:\d+(?:\.\d*)?|\.\d+)|inf))\s+dBFS", re.IGNORECASE
)
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", re.IGNORECASE)


def _parse_ffmpeg_ebur128(output: str) -> tuple[float, float, float]:
    loudness_matches = _FFMPEG_LOUDNESS_RE.findall(output)
    peak_matches = _FFMPEG_PEAK_RE.findall(output)
    if not loudness_matches or not peak_matches:
        raise ReplayGainAnalysisError("FFmpeg returned malformed loudness output")
    loudness = float(loudness_matches[-1])
    peak_dbfs = float(peak_matches[-1])
    if not math.isfinite(loudness) or peak_dbfs == math.inf or math.isnan(peak_dbfs):
        raise ReplayGainAnalysisError("FFmpeg returned non-finite loudness output")
    duration_match = _FFMPEG_DURATION_RE.search(output)
    duration = 1.0
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    peak = 0.0 if peak_dbfs == -math.inf else 10.0 ** (peak_dbfs / 20.0)
    return loudness, peak, duration


def _analyse_track_with_ffmpeg(
    path: Path,
    *,
    cancel_event: threading.Event | None = None,
) -> ReplayGainAnalysis:
    """Streaming, read-only FFmpeg boundary with deterministic cleanup."""
    from utils.paths import get_ffmpeg_executable

    executable = get_ffmpeg_executable()
    if not executable:
        raise ReplayGainAnalysisError(
            "ReplayGain analysis requires the bundled FFmpeg runtime or pyloudnorm+soundfile"
        )
    if cancel_event is not None and cancel_event.is_set():
        raise ReplayGainAnalysisCancelled("cancelled")
    command = [
        executable,
        "-hide_banner",
        "-nostats",
        "-i", str(path),
        "-filter_complex", "ebur128=peak=true",
        "-f", "null",
        "-",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ReplayGainAnalysisError(f"could not start FFmpeg: {exc}") from exc
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.05):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    raise ReplayGainAnalysisCancelled("cancelled")
                time.sleep(0.01)
            return_code = process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        diagnostics.seek(0)
        output = diagnostics.read().decode("utf-8", errors="replace")
    if return_code != 0:
        detail = output.strip().splitlines()[-1] if output.strip() else f"exit code {return_code}"
        raise ReplayGainAnalysisError(f"FFmpeg could not analyze {path.name}: {detail}")
    loudness, peak, duration = _parse_ffmpeg_ebur128(output)
    return ReplayGainAnalysis(
        path=path,
        track_gain_db=_REFERENCE_LUFS - loudness,
        track_peak=peak,
        loudness_lufs=loudness,
        duration_seconds=max(duration, 1e-9),
    )


def analyse_album(
    paths: Iterable[str | Path],
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int, ReplayGainAnalysis], None] | None = None,
) -> tuple[ReplayGainAnalysis, ...]:
    """Analyze one declared album and return track plus album proposals."""
    path_list = [Path(path) for path in paths]
    results: list[ReplayGainAnalysis] = []
    for index, path in enumerate(path_list, start=1):
        result = analyse_track(path, cancel_event=cancel_event)
        results.append(result)
        if progress is not None:
            progress(index, len(path_list), result)
    if not results:
        return ()
    album_loudness, album_peak = analyse_album_program(
        path_list, cancel_event=cancel_event
    )
    album_gain = _REFERENCE_LUFS - album_loudness
    return tuple(
        ReplayGainAnalysis(
            path=result.path,
            track_gain_db=result.track_gain_db,
            track_peak=result.track_peak,
            loudness_lufs=result.loudness_lufs,
            duration_seconds=result.duration_seconds,
            album_gain_db=album_gain,
            album_peak=album_peak,
        )
        for result in results
    )


def analyse_album_program(
    paths: Iterable[str | Path],
    *,
    cancel_event: threading.Event | None = None,
) -> tuple[float, float]:
    """Analyze ordered tracks as one continuous, streaming album programme."""
    from utils.paths import get_ffmpeg_executable

    path_list = [Path(path) for path in paths]
    if not path_list:
        raise ReplayGainAnalysisError("album contains no tracks")
    for path in path_list:
        if not path.exists() or not path.is_file():
            raise ReplayGainAnalysisError(f"file not found: {path}")
    if cancel_event is not None and cancel_event.is_set():
        raise ReplayGainAnalysisCancelled("cancelled")
    executable = get_ffmpeg_executable()
    if not executable:
        raise ReplayGainAnalysisError("album analysis requires the bundled FFmpeg runtime")

    command = [executable, "-hide_banner", "-nostats"]
    for path in path_list:
        command.extend(["-i", str(path)])
    normalizers = [
        f"[{index}:a:0]aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo"
        f"[album{index}]"
        for index in range(len(path_list))
    ]
    inputs = "".join(f"[album{index}]" for index in range(len(path_list)))
    filter_graph = ";".join(normalizers + [
        f"{inputs}concat=n={len(path_list)}:v=0:a=1,ebur128=peak=true[albumout]"
    ])
    command.extend([
        "-filter_complex", filter_graph,
        "-map", "[albumout]",
        "-f", "null", "-",
    ])
    output = _run_ffmpeg(command, "album programme", cancel_event=cancel_event)
    loudness, peak, _duration = _parse_ffmpeg_ebur128(output)
    return loudness, peak


def combine_album_results(
    results: Iterable[ReplayGainAnalysis],
) -> tuple[ReplayGainAnalysis, ...]:
    """Compatibility helper for the degenerate one-track album only.

    Multi-track album loudness cannot be reconstructed from independently
    gated track results; callers must use :func:`analyse_album` instead.
    """
    result_list = list(results)
    if not result_list:
        return ()
    if len(result_list) != 1:
        raise ReplayGainAnalysisError(
            "multi-track album loudness requires whole-programme analysis"
        )
    result = result_list[0]
    return (
        ReplayGainAnalysis(
            path=result.path,
            track_gain_db=result.track_gain_db,
            track_peak=result.track_peak,
            loudness_lufs=result.loudness_lufs,
            duration_seconds=result.duration_seconds,
            album_gain_db=_REFERENCE_LUFS - result.loudness_lufs,
            album_peak=result.track_peak,
        ),
    )


def _run_ffmpeg(
    command: list[str],
    label: str,
    *,
    cancel_event: threading.Event | None = None,
) -> str:
    """Run one read-only FFmpeg analysis with bounded cancellation cleanup."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise ReplayGainAnalysisError(f"could not start FFmpeg: {exc}") from exc
        try:
            while process.poll() is None:
                if cancel_event is not None and cancel_event.wait(0.05):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                    raise ReplayGainAnalysisCancelled("cancelled")
                time.sleep(0.01)
            return_code = process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        diagnostics.seek(0)
        output = diagnostics.read().decode("utf-8", errors="replace")
    if return_code != 0:
        detail = output.strip().splitlines()[-1] if output.strip() else f"exit code {return_code}"
        raise ReplayGainAnalysisError(f"FFmpeg could not analyze {label}: {detail}")
    return output


def analyse_and_embed(file_path: str) -> bool:
    """
    Analyse the audio file at file_path and embed ReplayGain tags.

    Returns True on success, False on failure (error is logged).
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning("[ReplayGain] File not found: %s", file_path)
        return False

    # Prefer rsgain CLI (fast, accurate, cross-platform binary)
    if shutil.which("rsgain"):
        return _analyse_with_rsgain(path)

    # Fall back to pyloudnorm + soundfile
    return _analyse_with_pyloudnorm(path)


# ── rsgain backend ─────────────────────────────────────────────────────────────

def _analyse_with_rsgain(path: Path) -> bool:
    """
    Use the `rsgain` CLI to compute and write ReplayGain tags in-place.

    rsgain easy -q FILE  writes tags directly; no Python tag-writing needed.
    """
    try:
        from utils.proc import run_hidden

        result = run_hidden(
            ["rsgain", "easy", "-q", str(path)],
            purpose="replaygain-rsgain",
            timeout=120,
        )
        if result.returncode == 0:
            logger.info("[ReplayGain] rsgain tagged %s", path.name)
            return True
        logger.warning(
            "[ReplayGain] rsgain failed (rc=%d): %s",
            result.returncode,
            result.stderr,
        )
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("[ReplayGain] rsgain error: %s", exc)
        return False


# ── pyloudnorm backend ─────────────────────────────────────────────────────────

_analysis_lock = threading.Lock()
_MAX_FILE_SIZE_BYTES = 150 * 1024 * 1024  # 150 MB


def _analyse_with_pyloudnorm(path: Path) -> bool:
    """
    Use pyloudnorm + soundfile to compute gain and embed tags manually.

    Requirements (installed separately):
        pip install pyloudnorm soundfile
    """
    # 1. File size check to prevent OOM
    try:
        file_size = path.stat().st_size
        if file_size > _MAX_FILE_SIZE_BYTES:
            logger.warning(
                "[ReplayGain] Skip Python ReplayGain analysis for '%s' (%.1f MB > 150 MB) to prevent RAM spikes. "
                "Please install 'rsgain' (CLI tool) for processing large files.",
                path.name,
                file_size / (1024 * 1024)
            )
            return False
    except Exception as exc:
        logger.debug("[ReplayGain] Failed to check size of %s: %s", path.name, exc)

    try:
        import soundfile as sf          # type: ignore[import]
        import pyloudnorm as pyln       # type: ignore[import]
    except ImportError:
        logger.warning(
            "[ReplayGain] Neither rsgain nor pyloudnorm+soundfile found. "
            "Install rsgain or run: pip install pyloudnorm soundfile"
        )
        return False

    # 2. Acquire global lock before reading file to RAM and executing analysis
    with _analysis_lock:
        try:
            data, rate = sf.read(str(path), always_2d=True)
        except Exception as exc:
            logger.error("[ReplayGain] soundfile read error on %s: %s", path.name, exc)
            return False

        try:
            meter     = pyln.Meter(rate)
            loudness  = meter.integrated_loudness(data)
            gain_db   = _REFERENCE_LUFS - loudness

            # Peak: max absolute sample value across all channels
            import numpy as np  # soundfile already requires numpy
            peak = float(np.abs(data).max())

            _write_tags(path, gain_db, peak)
            logger.info(
                "[ReplayGain] Tagged %s: gain=%.2f dB, peak=%.6f",
                path.name, gain_db, peak,
            )
            return True
        except Exception as exc:
            logger.error("[ReplayGain] Analysis error on %s: %s", path.name, exc)
            return False


# ── Tag writers ────────────────────────────────────────────────────────────────

def _write_tags(path: Path, gain_db: float, peak: float) -> None:
    """Embed REPLAYGAIN_TRACK_GAIN and REPLAYGAIN_TRACK_PEAK into the file."""
    from core.metadata_backend import METADATA_BACKEND
    METADATA_BACKEND.write_auxiliary(path, "replaygain", {
        "REPLAYGAIN_TRACK_GAIN": f"{gain_db:+.2f} dB",
        "REPLAYGAIN_TRACK_PEAK": f"{peak:.6f}",
    })
