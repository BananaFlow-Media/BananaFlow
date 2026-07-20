"""Converter edge cases the Phase 3 plan named but no test ever covered.

The converter-hardening review lists the required
converter test set. Three entries were never written and, unlike the
other gaps in that phase, were not disclosed in the phase report's
"Limitations and notes" either -- so they read as covered when they were
not:

* **read-only files** -- the repo has read-only tests, but for the Tag
  Editor only; nothing exercised the converter against them.
* **network paths where testable** -- nothing at all.
* **shutdown tests** -- cancellation was tested, but not the forced-kill
  fallback for a process that ignores the graceful stop, and not
  cancellation arriving between batch files or during verification.

## What this file actually covers, and what it does not

Read-only: real filesystem, no mocking. Note that a read-only *directory*
only blocks creation on POSIX -- on Windows the read-only attribute on a
directory is ignored for child creation (verified empirically, not
assumed), so that test skips there and a monkeypatched equivalent covers
the classification path on every platform. The read-only *destination
file* case is real on both.

Network paths: **path-shape coverage only.** These tests do not touch a
real SMB/CIFS share. They assert that UNC-shaped paths survive
destination resolution unmangled, with filesystem probing stubbed out --
deliberately, because pointing `Path.exists()` at a non-existent host
makes Windows block on network resolution and would make the suite slow
and flaky. Real share semantics (latency, locking, reconnects,
`os.replace` across a mount) are **not** covered here. A genuinely real
test is available opt-in: set `BANANAFLOW_TEST_UNC_DIR` to a writable share
path and the test at the bottom runs the full pipeline against it.

Shutdown: fakes, because a real FFmpeg that ignores CTRL_BREAK cannot be
summoned on demand. The fakes model the two behaviours that matter --
stopping on the graceful signal, and ignoring it so the kill fallback has
to fire.
"""

from __future__ import annotations

import os
import stat
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
    _stop_process,
)
from utils.paths import get_ffmpeg_executable, get_ffprobe_executable

FFMPEG = get_ffmpeg_executable()
FFPROBE = get_ffprobe_executable()
needs_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available",
)
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows-specific path semantics")
posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="a read-only directory does not block child creation on Windows",
)


def _service() -> ConverterService:
    return ConverterService(ffmpeg_path=FFMPEG, ffprobe_path=FFPROBE)


def _make_wav(path: Path, seconds: float = 1.0) -> Path:
    subprocess.run(
        [
            FFMPEG, "-nostdin", "-hide_banner", "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-codec:a", "pcm_s16le", "-y", str(path),
        ],
        capture_output=True, timeout=120, check=True,
    )
    return path


def _make_writable(*paths: Path) -> None:
    """Restore write permission so tmp_path cleanup cannot fail."""
    for path in paths:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Read-only files and directories
# ──────────────────────────────────────────────────────────────────────────────


@needs_ffmpeg
class TestReadOnlyInputsAndOutputs:

    def test_read_only_source_converts_and_is_left_untouched(self, tmp_path):
        """Converting reads the source; it must neither need write access
        nor modify it. A user converting files on read-only media is the
        real case behind this."""
        src = _make_wav(tmp_path / "readonly-source.wav")
        original = src.read_bytes()
        os.chmod(src, stat.S_IREAD)
        try:
            result = _service().convert_file(
                ConversionRequest(
                    source=src, output_format="mp3", bitrate="192k",
                    output_dir=tmp_path / "out",
                    collision_policy=CollisionPolicy.UNIQUE,
                )
            )
            assert result.outcome is FileOutcome.COMPLETED, result.message
            assert Path(result.destination).is_file()
            assert src.read_bytes() == original, "the source must not be rewritten"
            assert not (os.stat(src).st_mode & stat.S_IWRITE), (
                "the source's read-only attribute must survive the conversion"
            )
        finally:
            _make_writable(src)

    def test_overwrite_onto_a_read_only_destination_fails_and_preserves_it(self, tmp_path):
        """The dangerous case: an explicit OVERWRITE whose atomic replace
        cannot succeed. The existing file must survive byte-for-byte and no
        .part temp may be left behind."""
        src = _make_wav(tmp_path / "song.wav")
        dest = tmp_path / "song.mp3"
        dest.write_bytes(b"PRE-EXISTING CONTENT THAT MUST SURVIVE")
        untouched = dest.read_bytes()
        os.chmod(dest, stat.S_IREAD)
        try:
            result = _service().convert_file(
                ConversionRequest(
                    source=src, output_format="mp3", bitrate="192k",
                    collision_policy=CollisionPolicy.OVERWRITE,
                )
            )
            assert result.outcome is FileOutcome.FAILED
            assert result.error_kind is ConversionErrorKind.PERMISSION_DENIED, (
                f"a read-only destination must classify as permission denied, "
                f"got {result.error_kind}"
            )
            assert dest.read_bytes() == untouched, (
                "a failed overwrite must not damage the existing destination"
            )
            assert not list(tmp_path.glob("*.part.*")), "temp must be cleaned up"
        finally:
            _make_writable(dest)

    @posix_only
    def test_read_only_destination_directory_is_reported_not_crashed(self, tmp_path):
        src = _make_wav(tmp_path / "song.wav")
        out = tmp_path / "locked"
        out.mkdir()
        os.chmod(out, stat.S_IRUSR | stat.S_IXUSR)
        try:
            result = _service().convert_file(
                ConversionRequest(
                    source=src, output_format="mp3", bitrate="192k",
                    output_dir=out, collision_policy=CollisionPolicy.UNIQUE,
                )
            )
            assert result.outcome is FileOutcome.FAILED
            assert result.error_kind is ConversionErrorKind.PERMISSION_DENIED
        finally:
            os.chmod(out, stat.S_IRWXU)


class TestUnwritableDestinationClassification:
    """Platform-independent cover for the same failure the POSIX test above
    exercises for real, so the classification path is asserted on Windows
    too rather than silently skipped."""

    def test_mkdir_permission_error_maps_to_permission_denied(self, monkeypatch, tmp_path):
        src = tmp_path / "song.wav"
        src.write_bytes(b"x" * 128)
        service = ConverterService(ffmpeg_path="FF", ffprobe_path="FP")
        monkeypatch.setattr(
            ConverterService, "probe",
            lambda self, path, timeout=60.0: {
                "format": {"duration": "5.0", "format_name": "wav"},
                "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le"}],
            },
        )

        def _refuse(self, *args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", _refuse)
        result = service.convert_file(
            ConversionRequest(
                source=src, output_format="mp3", bitrate="192k",
                output_dir=tmp_path / "nope",
                collision_policy=CollisionPolicy.UNIQUE,
            )
        )
        assert result.outcome is FileOutcome.FAILED
        assert result.error_kind is ConversionErrorKind.PERMISSION_DENIED
        assert not list(tmp_path.glob("*.part.*"))


# ──────────────────────────────────────────────────────────────────────────────
# UNC / network-style paths  (path shape only -- see the module docstring)
# ──────────────────────────────────────────────────────────────────────────────


@windows_only
class TestUncStylePathShape:
    """No real share is touched. `Path.exists` is stubbed precisely so that
    Windows never tries to resolve a host name -- doing otherwise makes the
    suite hang on network resolution."""

    def test_unc_output_dir_survives_destination_resolution(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "exists", lambda self: False)
        src = tmp_path / "song.wav"
        request = ConversionRequest(
            source=src, output_format="mp3",
            output_dir=Path(r"\\fileserver\music\converted"),
            collision_policy=CollisionPolicy.UNIQUE,
        )
        dest, early = ConverterService(
            ffmpeg_path="FF", ffprobe_path="FP",
        ).resolve_destination(request)

        assert early is None
        assert str(dest).startswith("\\\\fileserver\\music\\converted"), (
            f"a UNC destination must stay UNC, got {dest!r} -- a mangled "
            f"prefix would silently write to a local path instead of the share"
        )
        assert dest.name == "song.mp3"

    def test_unc_collision_renaming_keeps_the_share_prefix(self, monkeypatch):
        from core.converter_service import _unique_destination

        monkeypatch.setattr(Path, "exists", lambda self: False)
        monkeypatch.setattr("core.converter_service._same_file", lambda a, b: False)

        dest = Path(r"\\fileserver\music\track.mp3")
        unique = _unique_destination(dest, Path(r"\\fileserver\music\track.wav"))
        assert str(unique).startswith("\\\\fileserver\\music\\")
        assert unique.name == "track (2).mp3"


@needs_ffmpeg
@pytest.mark.skipif(
    not os.environ.get("BANANAFLOW_TEST_UNC_DIR"),
    reason="set BANANAFLOW_TEST_UNC_DIR to a writable network share for real coverage",
)
def test_real_network_share_round_trip(tmp_path):
    """Opt-in, and the only test here that touches a real share.

    Everything else in this file is path-shape coverage; this is the one
    that exercises real remote `os.replace`, latency and locking. It is
    skipped by default because CI has no share to point at -- claiming
    otherwise would be exactly the kind of overstated coverage this file
    exists to correct.
    """
    share = Path(os.environ["BANANAFLOW_TEST_UNC_DIR"])
    src = _make_wav(tmp_path / "share-src.wav")
    result = _service().convert_file(
        ConversionRequest(
            source=src, output_format="mp3", bitrate="192k",
            output_dir=share, collision_policy=CollisionPolicy.UNIQUE,
        )
    )
    try:
        assert result.outcome is FileOutcome.COMPLETED, result.message
        produced = Path(result.destination)
        assert produced.is_file() and produced.stat().st_size > 0
        assert not list(share.glob("*.part.*")), "no temp may be left on the share"
    finally:
        if result.destination:
            Path(result.destination).unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# Shutdown / cancellation during active operations
# ──────────────────────────────────────────────────────────────────────────────


class _StubbornPopen:
    """A process that ignores the graceful stop, like FFmpeg builds that
    do not handle CTRL_BREAK. Only `kill()` ends it, so this is what
    actually exercises the forced-kill fallback."""

    def __init__(self) -> None:
        self._stopped = threading.Event()
        self.pid = 515151
        self.returncode: int | None = None
        self.signalled = False
        self.terminated = False
        self.killed = False
        self.stdout = self._stream()
        self.stderr = self._stream()

    def _stream(self):
        class _S:
            def __init__(self, owner): self._owner = owner
            def __iter__(self):
                self._owner._stopped.wait(10.0)
                return iter(())
            def read(self): return b""
        return _S(self)

    def poll(self): return self.returncode

    def send_signal(self, _sig): self.signalled = True      # deliberately ignored
    def terminate(self): self.terminated = True             # deliberately ignored

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._stopped.set()

    def wait(self, timeout=None):
        if not self._stopped.wait(timeout if timeout is not None else 30.0):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)
        return self.returncode


class TestForcedKillFallback:

    def test_a_process_ignoring_the_graceful_stop_is_killed(self):
        """Phase 3 section C requires a forced-kill fallback and 'no orphan
        processes'. Every existing cancellation test uses a fake that dies
        on the first signal, so the fallback itself was never executed."""
        proc = _StubbornPopen()
        _stop_process(proc, grace_seconds=0.2)

        assert proc.signalled or proc.terminated, "a graceful stop must be tried first"
        assert proc.killed, "a process ignoring the graceful stop must be force-killed"
        assert proc.returncode is not None, "no orphan may be left running"

    def test_stopping_an_already_exited_process_is_a_no_op(self):
        proc = _StubbornPopen()
        proc.returncode = 0
        _stop_process(proc, grace_seconds=0.2)
        assert not proc.killed and not proc.signalled and not proc.terminated

    def test_cancel_is_idempotent(self):
        proc = _StubbornPopen()
        token = CancellationToken()
        token._register(proc)
        token.cancel()
        token.cancel()          # must not raise on an already-stopped process
        assert token.cancelled and proc.killed

    def test_cancel_racing_process_registration_still_stops_it(self):
        """`CancellationToken._register` re-checks the flag because a cancel
        can land between Popen returning and the token learning about it.
        Without that, the process would run on with nothing holding it."""
        proc = _StubbornPopen()
        token = CancellationToken()
        token.cancel()          # cancelled BEFORE the process is known
        token._register(proc)
        assert proc.killed, "a cancel that raced registration must still stop the process"


@needs_ffmpeg
class TestCancellationDuringActiveWork:

    def test_cancel_during_verification_discards_the_converted_output(self, tmp_path):
        """Cancel arrives after FFmpeg succeeded but before the result is
        committed. Deterministic: the cancel is fired from the progress
        callback the moment the pipeline reports the verifying stage."""
        src = _make_wav(tmp_path / "song.wav")
        dest_dir = tmp_path / "out"
        token = CancellationToken()

        def _on_progress(fp):
            if fp.stage == "verifying":
                token.cancel()

        result = _service().convert_file(
            ConversionRequest(
                source=src, output_format="mp3", bitrate="192k",
                output_dir=dest_dir, collision_policy=CollisionPolicy.UNIQUE,
            ),
            on_progress=_on_progress,
            cancel=token,
        )
        assert result.outcome is FileOutcome.CANCELLED
        assert not list(dest_dir.glob("*.mp3")), (
            "a cancelled conversion must not leave a finished file behind"
        )
        assert not list(dest_dir.glob("*.part.*")), "temp must be cleaned up"

    def test_cancel_between_files_cancels_the_remainder(self, tmp_path):
        """Phase 3 section C requires cancellation between files, not only
        mid-file. The first file completes; everything after it is reported
        CANCELLED rather than silently dropped from the results."""
        sources = [_make_wav(tmp_path / f"track{n}.wav") for n in range(3)]
        out = tmp_path / "out"
        token = CancellationToken()
        seen: list[FileOutcome] = []

        def _on_file_result(res):
            seen.append(res.outcome)
            token.cancel()      # cancel right after the first file lands

        batch = _service().convert_batch(
            [
                ConversionRequest(
                    source=s, output_format="mp3", bitrate="192k",
                    output_dir=out, collision_policy=CollisionPolicy.UNIQUE,
                )
                for s in sources
            ],
            on_file_result=_on_file_result,
            cancel=token,
        )

        assert len(batch.results) == 3, "every request must be accounted for"
        assert batch.results[0].outcome is FileOutcome.COMPLETED
        assert all(r.outcome is FileOutcome.CANCELLED for r in batch.results[1:])
        assert batch.cancelled == 2
        assert not list(out.glob("*.part.*")), "no temp may survive a cancelled batch"
