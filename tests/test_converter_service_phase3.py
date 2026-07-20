"""Phase 3 converter-service hardening: unit + real-FFmpeg integration tests.

Unit tests exercise collision policy, error classification, command
construction, cancellation, timeout, and progress parsing with fakes.
Integration tests (skipped when FFmpeg/FFprobe are unavailable) run the
real pipeline end to end: atomic output, verification, metadata and
artwork preservation, corrupt input, no-audio input, and temp cleanup.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from core.converter_service import (
    CancellationToken,
    CollisionPolicy,
    ConversionErrorKind,
    ConversionRequest,
    ConverterService,
    FileOutcome,
    _classify_ffmpeg_stderr,
    _unique_destination,
    format_preservation_warnings,
)
from utils.paths import get_ffmpeg_executable, get_ffprobe_executable

FFMPEG = get_ffmpeg_executable()
FFPROBE = get_ffprobe_executable()
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available",
)


def _request(src: Path, fmt: str = "mp3", **kw) -> ConversionRequest:
    kw.setdefault("collision_policy", CollisionPolicy.UNIQUE)
    return ConversionRequest(source=src, output_format=fmt, bitrate="192k", **kw)


def _make_wav(path: Path, seconds: float = 1.0) -> Path:
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-codec:a", "pcm_s16le", "-y", str(path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    return path


def _make_tagged_mp3(path: Path, tmp_path: Path) -> Path:
    """A real MP3 with title/artist tags and embedded cover art."""
    wav = _make_wav(tmp_path / "tag-src.wav")
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-v", "error", "-i", str(wav),
        "-codec:a", "libmp3lame", "-b:a", "128k",
        "-metadata", "title=Phase3 Title", "-metadata", "artist=Phase3 Artist",
        "-y", str(path),
    ]
    subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    from mutagen.id3 import APIC, ID3
    tags = ID3(str(path))
    tags.add(APIC(encoding=3, mime="image/png", type=3, desc="Cover",
                  data=b"\x89PNG\r\n\x1a\n" + b"0" * 64))
    tags.save(str(path))
    return path


# ──────────────────────────────────────────────────────────────────────────────
# Unit: destination resolution and collision policy
# ──────────────────────────────────────────────────────────────────────────────


class TestDestinationResolution:

    def _service(self) -> ConverterService:
        return ConverterService(ffmpeg_path="ffmpeg-unused", ffprobe_path="ffprobe-unused")

    def test_same_format_same_folder_never_overwrites_source(self, tmp_path):
        src = tmp_path / "song.mp3"
        src.write_bytes(b"x")
        dest, early = self._service().resolve_destination(
            _request(src, "mp3", collision_policy=CollisionPolicy.OVERWRITE),
        )
        assert early is None
        assert dest is not None and dest != src
        assert dest.parent == tmp_path

    def test_skip_policy_returns_skipped_result(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        (tmp_path / "song.mp3").write_bytes(b"existing")
        dest, early = self._service().resolve_destination(
            _request(src, "mp3", collision_policy=CollisionPolicy.SKIP),
        )
        assert dest is None
        assert early is not None and early.outcome is FileOutcome.SKIPPED

    def test_unique_policy_generates_numbered_sibling(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        (tmp_path / "song.mp3").write_bytes(b"existing")
        dest, early = self._service().resolve_destination(
            _request(src, "mp3", collision_policy=CollisionPolicy.UNIQUE),
        )
        assert early is None
        assert dest == tmp_path / "song (2).mp3"

    def test_overwrite_policy_keeps_existing_destination_path(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        existing = tmp_path / "song.mp3"
        existing.write_bytes(b"existing")
        dest, early = self._service().resolve_destination(
            _request(src, "mp3", collision_policy=CollisionPolicy.OVERWRITE),
        )
        assert early is None
        assert dest == existing

    def test_ask_without_callback_fails_safe_to_skip(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        (tmp_path / "song.mp3").write_bytes(b"existing")
        dest, early = self._service().resolve_destination(
            _request(src, "mp3", collision_policy=CollisionPolicy.ASK),
        )
        assert dest is None
        assert early is not None
        assert early.outcome is FileOutcome.SKIPPED
        assert early.error_kind is ConversionErrorKind.COLLISION_UNRESOLVED

    def test_ask_callback_decision_is_honoured(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        (tmp_path / "song.mp3").write_bytes(b"existing")
        dest, early = self._service().resolve_destination(
            _request(src, "mp3", collision_policy=CollisionPolicy.ASK),
            ask_callback=lambda s, d: CollisionPolicy.UNIQUE,
        )
        assert early is None
        assert dest == tmp_path / "song (2).mp3"

    def test_unique_destination_skips_source_case_variant(self, tmp_path):
        src = tmp_path / "SONG.mp3"
        src.write_bytes(b"x")
        dest = _unique_destination(tmp_path / "song.mp3", src)
        assert dest.name != "song.mp3"


# ──────────────────────────────────────────────────────────────────────────────
# Unit: error classification, command construction, guards
# ──────────────────────────────────────────────────────────────────────────────


class TestClassificationAndCommand:

    @pytest.mark.parametrize("stderr,expected", [
        ("Stream map '0:a:0' matches no streams.", ConversionErrorKind.NO_AUDIO_STREAM),
        ("Invalid data found when processing input", ConversionErrorKind.CORRUPT_SOURCE),
        ("Unknown encoder 'libfoo'", ConversionErrorKind.UNSUPPORTED_CODEC),
        ("Permission denied", ConversionErrorKind.PERMISSION_DENIED),
        ("No space left on device", ConversionErrorKind.DISK_FULL),
        ("something completely else", ConversionErrorKind.UNKNOWN),
    ])
    def test_stderr_classification(self, stderr, expected):
        assert _classify_ffmpeg_stderr(stderr) is expected

    def test_command_shape_for_lossy_target(self, tmp_path):
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        cmd = service._build_command(
            _request(tmp_path / "a.wav", "mp3"), tmp_path / "a.wav", tmp_path / "t.part.mp3",
        )
        assert cmd[0] == "FF"
        assert "-nostdin" in cmd
        assert "-map_metadata" in cmd and cmd[cmd.index("-map_metadata") + 1] == "0"
        assert "-b:a" in cmd
        assert "-progress" in cmd and cmd[cmd.index("-progress") + 1] == "pipe:1"
        assert cmd[-1].endswith("t.part.mp3")
        assert cmd[cmd.index("-y") + 1] == cmd[-1], "-y may only apply to the temp file"

    @pytest.mark.parametrize("fmt", ["flac", "wav"])
    def test_lossless_targets_have_no_bitrate(self, tmp_path, fmt):
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        cmd = service._build_command(
            _request(tmp_path / "a.mp3", fmt), tmp_path / "a.mp3", tmp_path / f"t.part.{fmt}",
        )
        assert "-b:a" not in cmd

    def test_missing_ffmpeg_is_reported_not_raised(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        service = ConverterService(ffmpeg_path=None, ffprobe_path="FP")
        service._ffmpeg = None
        result = service.convert_file(_request(src))
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind is ConversionErrorKind.FFMPEG_MISSING

    def test_missing_source_is_reported(self, tmp_path):
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        result = service.convert_file(_request(tmp_path / "missing.wav"))
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind is ConversionErrorKind.SOURCE_MISSING

    def test_precancelled_token_short_circuits(self, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x")
        token = CancellationToken()
        token.cancel()
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        result = service.convert_file(_request(src), cancel=token)
        assert result.outcome is FileOutcome.CANCELLED
        assert result.error_kind is ConversionErrorKind.CANCELLED

    def test_wav_target_reports_preservation_loss(self):
        assert "tags" in format_preservation_warnings("wav")
        assert "artwork" in format_preservation_warnings("wav")
        assert format_preservation_warnings("mp3") == ()


# ──────────────────────────────────────────────────────────────────────────────
# Unit: cancellation and timeout against a fake process
# ──────────────────────────────────────────────────────────────────────────────


class _FakeStream:
    """Blocking line iterator that releases only when the fake proc stops."""

    def __init__(self, stopped: threading.Event, lines: list[bytes]):
        self._stopped = stopped
        self._lines = lines

    def __iter__(self):
        yield from self._lines
        self._stopped.wait(10.0)

    def read(self):
        return b""


class _FakePopen:
    def __init__(self, lines: list[bytes] | None = None):
        self._stopped = threading.Event()
        self.stdout = _FakeStream(self._stopped, lines or [])
        self.stderr = _FakeStream(self._stopped, [])
        self.pid = 424242
        self.returncode: int | None = None

    def poll(self):
        return self.returncode

    def send_signal(self, _sig):
        self._finish(-2)

    def terminate(self):
        self._finish(-15)

    def kill(self):
        self._finish(-9)

    def _finish(self, code: int):
        self.returncode = code
        self._stopped.set()

    def wait(self, timeout=None):
        if not self._stopped.wait(timeout if timeout is not None else 30.0):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)
        return self.returncode


class TestCancellationAndTimeout:

    def _service_with_fake(self, monkeypatch, tmp_path, fake: _FakePopen):
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        monkeypatch.setattr(
            "core.converter_service.subprocess.Popen", lambda *a, **k: fake,
        )
        # Source probe succeeds with a fixed duration and an audio stream.
        monkeypatch.setattr(
            ConverterService, "probe",
            lambda self, path, timeout=60.0: {
                "format": {"duration": "10.0", "format_name": "wav"},
                "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            },
        )
        return service

    def test_cancel_terminates_live_process_and_reports_cancelled(
        self, monkeypatch, tmp_path,
    ):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x" * 128)
        fake = _FakePopen()
        service = self._service_with_fake(monkeypatch, tmp_path, fake)
        token = CancellationToken()

        timer = threading.Timer(0.2, token.cancel)
        timer.start()
        try:
            result = service.convert_file(_request(src), cancel=token)
        finally:
            timer.cancel()
        assert result.outcome is FileOutcome.CANCELLED
        assert fake.returncode is not None, "cancel must stop the FFmpeg process"
        assert not list(tmp_path.glob("*.part.*")), "temp must be cleaned after cancel"

    def test_timeout_stops_stalled_process_with_no_output(self, monkeypatch, tmp_path):
        # The fake process emits NO stdout lines at all — the exact stall
        # that an in-loop deadline check cannot see. The watchdog must
        # still stop the process and report TIMEOUT.
        src = tmp_path / "song.wav"
        src.write_bytes(b"x" * 128)
        fake = _FakePopen()
        service = self._service_with_fake(monkeypatch, tmp_path, fake)
        request = ConversionRequest(
            source=src, output_format="mp3", bitrate="192k",
            collision_policy=CollisionPolicy.UNIQUE, timeout_seconds=0.3,
        )
        result = service.convert_file(request)
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind is ConversionErrorKind.TIMEOUT
        assert fake.returncode is not None, "timeout must stop the FFmpeg process"
        assert not list(tmp_path.glob("*.part.*")), "temp must be cleaned after timeout"

    def test_progress_lines_reach_callback_with_real_ratio(self, monkeypatch, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x" * 128)
        lines = [b"out_time_us=2500000\n", b"out_time_us=5000000\n", b"progress=end\n"]
        fake = _FakePopen(lines=lines)
        threading.Timer(0.2, lambda: fake._finish(0)).start()
        service = self._service_with_fake(monkeypatch, tmp_path, fake)
        # Output verification would need a real file; force success path to
        # stop after the ffmpeg stage by failing verification predictably.
        seen: list[tuple[float, float | None]] = []
        result = service.convert_file(
            _request(src),
            on_progress=lambda fp: seen.append((fp.seconds_done, fp.ratio)),
        )
        converting = [s for s in seen if s[1] is not None and s[0] > 0]
        assert (2.5, 0.25) in converting
        assert (5.0, 0.5) in converting
        # Fake process produced no real file, so the pipeline must fail
        # closed at verification rather than fabricate success.
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind is ConversionErrorKind.INVALID_OUTPUT


# ──────────────────────────────────────────────────────────────────────────────
# Unit: OSError classification (locked / long-path / permission / disk-full)
# ──────────────────────────────────────────────────────────────────────────────


class TestOsErrorClassification:

    def _win_error(self, winerror: int) -> OSError:
        exc = OSError(22, "synthetic windows error")
        exc.winerror = winerror
        return exc

    def test_sharing_violation_maps_to_destination_locked(self):
        from core.converter_service import _classify_oserror
        assert _classify_oserror(self._win_error(32)) is ConversionErrorKind.DESTINATION_LOCKED
        assert _classify_oserror(self._win_error(33)) is ConversionErrorKind.DESTINATION_LOCKED

    def test_long_path_maps_to_path_too_long(self):
        import errno as _errno
        from core.converter_service import _classify_oserror
        assert _classify_oserror(self._win_error(206)) is ConversionErrorKind.PATH_TOO_LONG
        assert _classify_oserror(
            OSError(_errno.ENAMETOOLONG, "name too long"),
        ) is ConversionErrorKind.PATH_TOO_LONG

    def test_enospc_maps_to_disk_full(self):
        import errno as _errno
        from core.converter_service import _classify_oserror
        assert _classify_oserror(OSError(_errno.ENOSPC, "no space")) is ConversionErrorKind.DISK_FULL

    def test_permission_maps_to_permission_denied(self):
        import errno as _errno
        from core.converter_service import _classify_oserror
        assert _classify_oserror(PermissionError(_errno.EACCES, "denied")) is ConversionErrorKind.PERMISSION_DENIED

    def test_missing_file_maps_to_source_missing(self):
        from core.converter_service import _classify_oserror
        assert _classify_oserror(FileNotFoundError(2, "gone")) is ConversionErrorKind.SOURCE_MISSING


# ──────────────────────────────────────────────────────────────────────────────
# Unit: destination appearing between resolve and atomic replace
# ──────────────────────────────────────────────────────────────────────────────


class TestReplaceRaceSafety:
    """The destination is created *during* the conversion (after collision
    resolution but before os.replace) — the exact race the atomic step must
    survive without clobbering the newcomer."""

    def _racing_service(self, monkeypatch, race_dest: Path) -> ConverterService:
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        monkeypatch.setattr(
            ConverterService, "probe",
            lambda self, path, timeout=60.0: {
                "format": {"duration": "1.0", "format_name": "wav"},
                "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            },
        )

        def fake_run(self, request, src, temp, duration, cancel, progress):
            temp.write_bytes(b"converted-bytes")
            return None

        def fake_verify(self, request, temp, source_duration):
            race_dest.write_bytes(b"RACER")   # appears mid-conversion
            return None

        monkeypatch.setattr(ConverterService, "_run_ffmpeg", fake_run)
        monkeypatch.setattr(ConverterService, "_verify_output", fake_verify)
        monkeypatch.setattr(
            ConverterService, "_copy_artwork", classmethod(lambda cls, s, t, f: None),
        )
        return service

    def test_skip_policy_aborts_and_preserves_the_newcomer(self, monkeypatch, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x" * 64)
        race_dest = tmp_path / "song.mp3"
        service = self._racing_service(monkeypatch, race_dest)
        result = service.convert_file(
            _request(src, "mp3", collision_policy=CollisionPolicy.SKIP),
        )
        assert result.outcome is FileOutcome.SKIPPED
        assert result.error_kind is ConversionErrorKind.COLLISION_UNRESOLVED
        assert race_dest.read_bytes() == b"RACER", "newcomer must not be replaced"
        assert not list(tmp_path.glob("*.part.*")), "temp must be cleaned"

    def test_unique_policy_renumbers_and_preserves_the_newcomer(self, monkeypatch, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x" * 64)
        race_dest = tmp_path / "song.mp3"
        service = self._racing_service(monkeypatch, race_dest)
        result = service.convert_file(
            _request(src, "mp3", collision_policy=CollisionPolicy.UNIQUE),
        )
        assert result.outcome is FileOutcome.COMPLETED
        assert Path(result.destination).name == "song (2).mp3"
        assert Path(result.destination).read_bytes() == b"converted-bytes"
        assert race_dest.read_bytes() == b"RACER", "newcomer must not be replaced"
        assert not list(tmp_path.glob("*.part.*")), "temp must be cleaned"


# ──────────────────────────────────────────────────────────────────────────────
# Integration: real FFmpeg end-to-end
# ──────────────────────────────────────────────────────────────────────────────


@needs_ffmpeg
class TestRealFfmpegPipeline:

    def test_wav_to_mp3_completes_verifies_and_cleans_up(self, tmp_path):
        src = _make_wav(tmp_path / "tone.wav")
        source_bytes = src.read_bytes()
        progress: list[float] = []
        result = ConverterService().convert_file(
            _request(src, "mp3"),
            on_progress=lambda fp: progress.append(fp.seconds_done),
        )
        assert result.outcome is FileOutcome.COMPLETED, result.message
        dest = Path(result.destination)
        assert dest.is_file() and dest.stat().st_size > 0
        assert src.read_bytes() == source_bytes, "source must remain untouched"
        assert not list(tmp_path.glob("*.part.*")), "no temp remnants allowed"
        assert progress, "real progress events must be emitted"

    @pytest.mark.parametrize("fmt", ["m4a", "flac", "opus", "wav"])
    def test_all_supported_targets_produce_verified_output(self, tmp_path, fmt):
        src = _make_wav(tmp_path / f"tone-{fmt}.wav")
        result = ConverterService().convert_file(_request(src, fmt))
        assert result.outcome is FileOutcome.COMPLETED, result.message
        assert Path(result.destination).stat().st_size > 0

    def test_metadata_and_artwork_survive_mp3_to_flac(self, tmp_path):
        pytest.importorskip("mutagen")
        src = _make_tagged_mp3(tmp_path / "tagged.mp3", tmp_path)
        result = ConverterService().convert_file(_request(src, "flac"))
        assert result.outcome is FileOutcome.COMPLETED, result.message
        from mutagen.flac import FLAC
        flac = FLAC(result.destination)
        assert flac.get("title") == ["Phase3 Title"]
        assert flac.get("artist") == ["Phase3 Artist"]
        assert flac.pictures, "embedded artwork must be carried over"
        assert result.artwork_preserved

    def test_corrupt_source_fails_closed_without_output(self, tmp_path):
        src = tmp_path / "corrupt.mp3"
        src.write_bytes(b"this is not audio at all" * 10)
        result = ConverterService().convert_file(_request(src, "flac"))
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind in (
            ConversionErrorKind.CORRUPT_SOURCE,
            ConversionErrorKind.NO_AUDIO_STREAM,
            ConversionErrorKind.INVALID_OUTPUT,
            ConversionErrorKind.UNKNOWN,
        )
        assert not (tmp_path / "corrupt.flac").exists()
        assert not list(tmp_path.glob("*.part.*"))

    def test_video_only_input_reports_no_audio_stream(self, tmp_path):
        video = tmp_path / "video.mp4"
        subprocess.run(
            [FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
             "-i", "color=c=black:s=64x64:d=1", "-an", "-y", str(video)],
            capture_output=True, timeout=120, check=True,
        )
        result = ConverterService().convert_file(_request(video, "mp3"))
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind is ConversionErrorKind.NO_AUDIO_STREAM

    def test_skip_policy_preserves_existing_destination_bytes(self, tmp_path):
        src = _make_wav(tmp_path / "tone.wav")
        existing = tmp_path / "tone.mp3"
        existing.write_bytes(b"KEEP ME")
        result = ConverterService().convert_file(
            _request(src, "mp3", collision_policy=CollisionPolicy.SKIP),
        )
        assert result.outcome is FileOutcome.SKIPPED
        assert existing.read_bytes() == b"KEEP ME"

    def test_overwrite_policy_replaces_only_after_verification(self, tmp_path):
        src = _make_wav(tmp_path / "tone.wav")
        existing = tmp_path / "tone.mp3"
        existing.write_bytes(b"OLD")
        result = ConverterService().convert_file(
            _request(src, "mp3", collision_policy=CollisionPolicy.OVERWRITE),
        )
        assert result.outcome is FileOutcome.COMPLETED
        assert existing.stat().st_size > 3, "verified output must replace the old file"

    def test_unique_policy_creates_numbered_file_end_to_end(self, tmp_path):
        src = _make_wav(tmp_path / "tone.wav")
        (tmp_path / "tone.mp3").write_bytes(b"existing")
        result = ConverterService().convert_file(
            _request(src, "mp3", collision_policy=CollisionPolicy.UNIQUE),
        )
        assert result.outcome is FileOutcome.COMPLETED
        assert Path(result.destination).name == "tone (2).mp3"

    def test_same_extension_conversion_never_touches_source(self, tmp_path):
        src = _make_wav(tmp_path / "tone.wav")
        before = src.read_bytes()
        result = ConverterService().convert_file(
            _request(src, "wav", collision_policy=CollisionPolicy.OVERWRITE),
        )
        assert result.outcome is FileOutcome.COMPLETED
        assert Path(result.destination) != src
        assert src.read_bytes() == before

    def test_hebrew_and_unicode_filenames(self, tmp_path):
        src = _make_wav(tmp_path / "שיר בדיקה — ‎naïve.wav")
        result = ConverterService().convert_file(_request(src, "mp3"))
        assert result.outcome is FileOutcome.COMPLETED, result.message
        assert Path(result.destination).exists()

    def test_batch_counts_and_partial_failure(self, tmp_path):
        good = _make_wav(tmp_path / "good.wav")
        bad = tmp_path / "bad.wav"
        bad.write_bytes(b"garbage")
        service = ConverterService()
        batch = service.convert_batch([
            _request(good, "mp3"),
            _request(bad, "mp3"),
        ])
        assert batch.completed == 1
        assert batch.failed == 1
        assert batch.skipped == 0

    def test_converter_smoke_mode_passes(self, capsys):
        from core.converter_smoke import run_converter_smoke
        assert run_converter_smoke() == 0
        out = capsys.readouterr().out
        assert "SMOKE PASS: converter" in out
