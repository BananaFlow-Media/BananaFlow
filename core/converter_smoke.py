"""
core/converter_smoke.py  –  Packaged converter smoke test
==========================================================
Hidden CLI mode (``--internal-smoke-test converter``) that proves the
packaged build can convert audio end to end with its own bundled FFmpeg:

1. creates a scratch workspace under the system temp directory
   (never the installation directory);
2. synthesizes a small sine-wave WAV fixture with the packaged FFmpeg;
3. converts it to MP3 through :class:`core.converter_service.ConverterService`;
4. asserts that real progress events were observed;
5. verifies the output through the service's FFprobe verification; and
6. asserts that no temp ``.part`` file is left behind.

Exit code 0 on success, 1 on any failure.  Output is plain text so CI
and manual acceptance can capture it as evidence.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from core.converter_service import (
    CollisionPolicy,
    ConversionRequest,
    ConverterService,
    FileOutcome,
)
from utils.paths import get_ffmpeg_executable, get_ffprobe_executable


def _fail(step: str, detail: str) -> int:
    print(f"SMOKE FAIL [{step}]: {detail}")
    return 1


def run_converter_smoke() -> int:
    ffmpeg = get_ffmpeg_executable()
    ffprobe = get_ffprobe_executable()
    print("BananaFlow converter smoke test")
    print(f"  ffmpeg:  {ffmpeg or 'NOT FOUND'}")
    print(f"  ffprobe: {ffprobe or 'NOT FOUND'}")
    if not ffmpeg:
        return _fail("discovery", "bundled/PATH ffmpeg not found")
    if not ffprobe:
        return _fail("discovery", "bundled/PATH ffprobe not found")

    with tempfile.TemporaryDirectory(prefix="bananaflow-smoke-") as workdir:
        work = Path(workdir)
        fixture = work / "fixture.wav"

        # 1. Synthesize a 2-second sine fixture with the same FFmpeg build.
        cmd = [
            ffmpeg, "-nostdin", "-hide_banner", "-v", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-codec:a", "pcm_s16le", "-y", str(fixture),
        ]
        kwargs = (
            {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        )
        proc = subprocess.run(cmd, capture_output=True, timeout=120, **kwargs)
        if proc.returncode != 0 or not fixture.is_file() or fixture.stat().st_size == 0:
            return _fail(
                "fixture",
                proc.stderr.decode(errors="replace")[-300:] or "fixture not created",
            )
        print(f"  fixture: {fixture.name} ({fixture.stat().st_size} bytes)")

        # 2. Convert through the real service.
        service = ConverterService(ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
        progress_events: list[float] = []
        result = service.convert_file(
            ConversionRequest(
                source=fixture,
                output_format="mp3",
                bitrate="192k",
                output_dir=work,
                collision_policy=CollisionPolicy.UNIQUE,
                timeout_seconds=120.0,
            ),
            on_progress=lambda fp: progress_events.append(fp.seconds_done),
        )
        if result.outcome is not FileOutcome.COMPLETED:
            return _fail(
                "convert", f"{result.error_kind.value}: {result.message[:300]}",
            )
        destination = Path(result.destination or "")
        print(f"  output:  {destination.name} ({destination.stat().st_size} bytes)")

        # 3. Progress must have been observed from the real pipeline.
        if not progress_events:
            return _fail("progress", "no progress events were emitted")
        print(f"  progress events: {len(progress_events)}")

        # 4. Source must be untouched and output non-empty (verification of
        #    codec/container/duration already ran inside the service).
        if not fixture.is_file() or fixture.stat().st_size == 0:
            return _fail("source", "source fixture was modified or removed")
        if destination.stat().st_size == 0:
            return _fail("output", "output file is empty")

        # 5. No temp remnants.
        leftovers = [p.name for p in work.glob("*.part.*")]
        if leftovers:
            return _fail("cleanup", f"temp files left behind: {leftovers}")

    print("SMOKE PASS: converter")
    return 0


if __name__ == "__main__":
    sys.exit(run_converter_smoke())
