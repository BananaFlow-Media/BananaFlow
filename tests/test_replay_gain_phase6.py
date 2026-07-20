"""Phase 6 ReplayGain model, mapping, grouping, and analysis tests."""
from __future__ import annotations

import shutil
import subprocess
import wave
import threading
import math
from array import array
from pathlib import Path

import pytest

from core.metadata_models import (
    AudioTrackItem,
    OriginalTags,
    ProposedTags,
    REPLAYGAIN_ALBUM_GAIN,
    REPLAYGAIN_ALBUM_PEAK,
    REPLAYGAIN_REFERENCE_LOUDNESS,
    REPLAYGAIN_TRACK_GAIN,
    REPLAYGAIN_TRACK_PEAK,
    parse_replaygain_number,
)
from core.metadata_processor import atomic_write_tags, read_tags
from core.replay_gain import (
    ReplayGainAnalysis,
    ReplayGainAnalysisError,
    ReplayGainAnalysisCancelled,
    _analyse_track_with_ffmpeg,
    _parse_ffmpeg_ebur128,
    analyse_album,
    analyse_album_program,
    analyse_track,
    combine_album_results,
    group_album_scope,
)
from tests.audio_fixtures import make_empty_audio


def _compressed(path: Path, codec: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg unavailable")
    result = subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
        "-t", "0.08", "-c:a", codec, "-y", str(path),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def _fixture(tmp_path: Path, extension: str) -> Path:
    path = tmp_path / f"רווח replay {extension[1:]}{extension}"
    if extension in {".mp3", ".flac", ".m4a", ".wav"}:
        make_empty_audio(path)
    elif extension == ".opus":
        _compressed(path, "libopus")
    else:
        _compressed(path, "libvorbis")
    return path


def _tone_wave(path: Path, duration: float, amplitude: float, *, rate: int = 48000) -> None:
    count = int(duration * rate)
    samples = array("h", (
        int(32767 * amplitude * math.sin(2 * math.pi * 440 * index / rate))
        for index in range(count)
    ))
    stereo = array("h")
    for sample in samples:
        stereo.extend((sample, sample))
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(stereo.tobytes())


def _concat_wave(target_path: Path, paths: list[Path]) -> None:
    frames = []
    params = None
    for path in paths:
        with wave.open(str(path), "rb") as source:
            current = (source.getnchannels(), source.getsampwidth(), source.getframerate())
            params = params or current
            assert current == params
            frames.append(source.readframes(source.getnframes()))
    with wave.open(str(target_path), "wb") as target:
        target.setnchannels(params[0])
        target.setsampwidth(params[1])
        target.setframerate(params[2])
        target.writeframes(b"".join(frames))


@pytest.mark.parametrize("raw,peak,expected", [
    ("+1.25 dB", False, 1.25),
    ("-7.00 dB", False, -7.0),
    ("0.998765", True, 0.998765),
    ("malformed", False, None),
    ("-1", True, None),
])
def test_replaygain_parsing_and_units(raw, peak, expected):
    assert parse_replaygain_number(raw, peak=peak) == expected


@pytest.mark.parametrize("extension", [".mp3", ".flac", ".m4a", ".opus", ".ogg", ".wav"])
def test_real_replaygain_set_clear_and_semantic_readback(tmp_path, extension):
    path = _fixture(tmp_path, extension)
    original = read_tags(path)
    proposal = ProposedTags()
    proposal.set_replay_gain(REPLAYGAIN_TRACK_GAIN, "+2.50 dB")
    proposal.set_replay_gain(REPLAYGAIN_TRACK_PEAK, "0.987654")
    proposal.set_replay_gain(REPLAYGAIN_ALBUM_GAIN, "-1.75 dB")
    proposal.set_replay_gain(REPLAYGAIN_ALBUM_PEAK, "0.999")
    proposal.set_replay_gain(REPLAYGAIN_REFERENCE_LOUDNESS, "89.0 dB")
    fields = atomic_write_tags(path, proposal, original)
    assert {
        REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK,
        REPLAYGAIN_ALBUM_GAIN, REPLAYGAIN_ALBUM_PEAK,
        REPLAYGAIN_REFERENCE_LOUDNESS,
    }.issubset(fields)
    stored = read_tags(path).replay_gain
    assert stored.track_gain_db == 2.5
    assert stored.track_peak == pytest.approx(0.987654)
    assert stored.album_gain_db == -1.75
    assert stored.album_peak == pytest.approx(0.999)
    assert stored.reference_loudness_db == 89.0

    clear = ProposedTags()
    clear.clear_replay_gain({REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK})
    atomic_write_tags(path, clear, read_tags(path))
    after = read_tags(path).replay_gain
    assert after.track_gain_db is None and after.track_peak is None
    assert after.album_gain_db == -1.75 and after.album_peak == pytest.approx(0.999)


def test_replaygain_edit_preserves_unrelated_custom_id3_tag(tmp_path):
    from mutagen.id3 import Encoding, ID3, TXXX

    path = _fixture(tmp_path, ".mp3")
    tags = ID3(str(path))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="CUSTOM_USER_TAG", text="keep"))
    tags.save(str(path))
    proposal = ProposedTags()
    proposal.set_replay_gain(REPLAYGAIN_TRACK_GAIN, -3.0)
    atomic_write_tags(path, proposal, read_tags(path))
    assert ID3(str(path))["TXXX:CUSTOM_USER_TAG"].text[0] == "keep"


def test_album_grouping_separates_albums_and_missing_metadata(tmp_path):
    def item(name, album, artist):
        path = tmp_path / name
        return AudioTrackItem(
            path=path, folder=tmp_path, ext=".mp3", format_id="mp3",
            original=OriginalTags(album=album, album_artist=artist),
        )
    groups = group_album_scope([
        item("a.mp3", "One", "Artist"), item("b.mp3", "One", "Artist"),
        item("c.mp3", "Two", "Artist"), item("d.mp3", "", ""),
    ])
    assert sorted(len(group.tracks) for group in groups) == [1, 1, 2]
    assert sum(group.ambiguous for group in groups) == 1


def test_analysis_returns_proposal_values_without_modifying_audio(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    path = tmp_path / "עברית path with spaces.wav"
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        # Non-silent samples avoid -inf integrated loudness.
        target.writeframes((b"\x00\x10\x00\xf0") * 4000)
    before = path.read_bytes()
    result = analyse_track(path)
    assert path.read_bytes() == before
    assert set(result.proposal_values()) == {
        REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK, REPLAYGAIN_REFERENCE_LOUDNESS,
    }
    assert result.proposal_values()[REPLAYGAIN_REFERENCE_LOUDNESS] == 89.0
    combined = combine_album_results([result])
    assert combined[0].album_gain_db is not None and combined[0].album_peak is not None


def test_ffmpeg_output_parser_uses_summary_and_rejects_malformed_output():
    output = """
      Duration: 00:02:03.50, start: 0.000000
      [Parsed_ebur128] I: -14.2 LUFS
      Integrated loudness:\n        I: -16.0 LUFS
      True peak:\n        Peak: -1.0 dBFS
    """
    loudness, peak, duration = _parse_ffmpeg_ebur128(output)
    assert loudness == -16.0
    assert peak == pytest.approx(10 ** (-1 / 20))
    assert duration == 123.5
    with pytest.raises(ReplayGainAnalysisError):
        _parse_ffmpeg_ebur128("unexpected analyzer text")


def test_ffmpeg_cancellation_terminates_process_and_uses_resolved_executable(tmp_path, monkeypatch):
    path = tmp_path / "עברית path with spaces.wav"
    path.write_bytes(b"fixture")
    captured = {}

    class Process:
        returncode = None
        terminated = False
        killed = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 1

        def kill(self):
            self.killed = True
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    def popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    class CancelAfterStart(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: r"C:\bundled runtime\ffmpeg.exe")
    monkeypatch.setattr("core.replay_gain.subprocess.Popen", popen)
    with pytest.raises(ReplayGainAnalysisCancelled):
        _analyse_track_with_ffmpeg(path, cancel_event=CancelAfterStart())
    assert captured["command"][0] == r"C:\bundled runtime\ffmpeg.exe"
    assert str(path) in captured["command"]
    assert captured["kwargs"]["shell"] is False
    assert process.terminated and not process.killed


@pytest.mark.parametrize("raw,peak,expected", [
    ("+3.00 dB", False, 3.0),
    ("-7.25 dB", False, -7.25),
    ("  +3.00 dB\t", False, 3.0),
    ("0", True, 0.0),
    ("0.987654", True, 0.987654),
    ("+1.0", True, 1.0),
])
def test_replaygain_full_string_parser_accepts_only_declared_valid_syntax(raw, peak, expected):
    assert parse_replaygain_number(raw, peak=peak) == expected


@pytest.mark.parametrize("raw,peak", [
    ("prefix +3.00 dB", False),
    ("+3.00 dB suffix", False),
    ("+3.00 -2.00 dB", False),
    ("NaN dB", False),
    ("inf dB", False),
    ("-inf dB", False),
    (float("nan"), False),
    (float("inf"), False),
    (float("-inf"), True),
    ("3.00 volts", False),
    ("3.00", False),
    ("3.00 dB", True),
    ("", False),
    ("junk3.00", True),
    ("3,00 dB", False),
])
def test_replaygain_full_string_parser_rejects_malformed_values(raw, peak):
    assert parse_replaygain_number(raw, peak=peak) is None


def test_malformed_existing_replaygain_is_diagnostic_not_canonical(tmp_path):
    from mutagen.id3 import Encoding, ID3, TXXX

    path = _fixture(tmp_path, ".mp3")
    tags = ID3(str(path))
    raw = "corrupt-prefix +3.00 dB corrupt-suffix"
    tags.add(TXXX(encoding=Encoding.UTF8, desc="REPLAYGAIN_TRACK_GAIN", text=raw))
    tags.save(str(path))
    stored = read_tags(path)
    assert stored.replay_gain.track_gain_db is None
    assert stored.file_properties["invalid_replaygain"][REPLAYGAIN_TRACK_GAIN] == raw


def test_album_grouping_is_conservative_and_deduplicates_workspace_identity(tmp_path):
    def item(name, album="Greatest Hits", artist="", album_artist="", year=""):
        path = tmp_path / name
        return AudioTrackItem(
            path=path, folder=tmp_path, ext=".mp3", format_id="mp3",
            original=OriginalTags(
                album=album, artist=artist, album_artist=album_artist, year=year,
            ),
        )

    blank_a, blank_b = item("blank-a.mp3"), item("blank-b.mp3")
    blank_groups = group_album_scope([blank_a, blank_b])
    assert len(blank_groups) == 2 and all(group.ambiguous for group in blank_groups)
    assert all(len(group.tracks) == 1 for group in blank_groups)

    same_a = item("same-a.mp3", album_artist="Band", year="2001")
    same_b = item("same-b.mp3", artist="Other", album_artist="Band", year="2001")
    stable_ids = {id(same_a): 1, id(same_b): 2}
    groups = group_album_scope(
        [same_a, same_b, same_a], item_id=lambda track: stable_ids[id(track)]
    )
    assert len(groups) == 1 and groups[0].tracks == (same_a, same_b)
    assert not groups[0].ambiguous

    different_artist = item("different.mp3", artist="Solo", year="2001")
    assert len(group_album_scope([same_a, different_artist])) == 2
    different_year = item("later.mp3", album_artist="Band", year="2024")
    assert len(group_album_scope([same_a, different_year])) == 2

    missing_year_a = item("year-a.mp3", album_artist="Band")
    missing_year_b = item("year-b.mp3", album_artist="Band")
    assert len(group_album_scope([missing_year_a, missing_year_b])) == 1
    assert len(group_album_scope([missing_year_a, same_a])) == 2


def test_album_analysis_matches_independent_concatenated_programme_oracle(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    loud = tmp_path / "01 loud.wav"
    quiet = tmp_path / "02 quiet.wav"
    oracle_path = tmp_path / "independent oracle.wav"
    _tone_wave(loud, 4.0, 0.7)
    _tone_wave(quiet, 20.0, 0.01)
    _concat_wave(oracle_path, [loud, quiet])
    before = {path: path.read_bytes() for path in (loud, quiet)}

    production = analyse_album([loud, quiet])
    oracle = _analyse_track_with_ffmpeg(oracle_path)
    assert production[0].album_gain_db == pytest.approx(-18.0 - oracle.loudness_lufs, abs=0.15)
    assert production[0].album_peak == pytest.approx(oracle.track_peak, rel=0.02)
    assert all(result.album_gain_db == production[0].album_gain_db for result in production)

    duration = sum(result.duration_seconds for result in production)
    invalid_loudness = 10.0 * math.log10(sum(
        10.0 ** (result.loudness_lufs / 10.0) * result.duration_seconds
        for result in production
    ) / duration)
    invalid_gain = -18.0 - invalid_loudness
    assert abs(invalid_gain - production[0].album_gain_db) > 3.0
    assert {path: path.read_bytes() for path in (loud, quiet)} == before

    reversed_oracle = tmp_path / "reversed oracle.wav"
    _concat_wave(reversed_oracle, [quiet, loud])
    reversed_production = analyse_album([quiet, loud])
    reversed_expected = _analyse_track_with_ffmpeg(reversed_oracle)
    assert reversed_production[0].album_gain_db == pytest.approx(
        -18.0 - reversed_expected.loudness_lufs, abs=0.15
    )


def test_album_analysis_handles_silence_and_very_short_tracks(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    silence = tmp_path / "silence.wav"
    short = tmp_path / "short.wav"
    _tone_wave(silence, 1.0, 0.0)
    _tone_wave(short, 0.02, 0.3)
    results = analyse_album([silence, short])
    assert len(results) == 2
    assert all(math.isfinite(result.track_gain_db) for result in results)
    assert all(math.isfinite(result.album_gain_db) for result in results)
    assert results[0].album_peak is not None and results[0].album_peak >= 0.0


def test_python_decoder_runtime_failure_falls_back_once(tmp_path, monkeypatch):
    path = _fixture(tmp_path, ".m4a")
    analysis = ReplayGainAnalysis(path, -1.0, 0.9, -17.0, 1.0)
    calls = []
    monkeypatch.setattr("core.replay_gain._python_analysis_stack", lambda: object())
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_python",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ReplayGainAnalysisError("unsupported AAC")),
    )
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_ffmpeg",
        lambda received, **_kwargs: (calls.append(received), analysis)[1],
    )
    assert analyse_track(path) is analysis
    assert calls == [path]


def test_python_decoder_success_never_starts_ffmpeg(tmp_path, monkeypatch):
    path = tmp_path / "python.wav"
    path.write_bytes(b"fixture")
    analysis = ReplayGainAnalysis(path, -1.0, 0.9, -17.0, 1.0)
    monkeypatch.setattr("core.replay_gain._python_analysis_stack", lambda: object())
    monkeypatch.setattr("core.replay_gain._analyse_track_with_python", lambda *_a, **_k: analysis)
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_ffmpeg",
        lambda *_a, **_k: pytest.fail("FFmpeg must not run"),
    )
    assert analyse_track(path) is analysis


def test_unavailable_python_stack_uses_ffmpeg_once_with_unicode_path(tmp_path, monkeypatch):
    path = tmp_path / "עברית fallback path.m4a"
    path.write_bytes(b"fixture")
    analysis = ReplayGainAnalysis(path, -1.0, 0.9, -17.0, 1.0)
    calls = []
    monkeypatch.setattr(
        "core.replay_gain._python_analysis_stack",
        lambda: (_ for _ in ()).throw(ImportError("unavailable")),
    )
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_ffmpeg",
        lambda received, **_kwargs: (calls.append(received), analysis)[1],
    )
    assert analyse_track(path) is analysis
    assert calls == [path]


def test_cancel_between_python_failure_and_fallback_does_not_start_ffmpeg(tmp_path, monkeypatch):
    path = tmp_path / "cancel.m4a"
    path.write_bytes(b"fixture")
    cancelled = threading.Event()
    monkeypatch.setattr("core.replay_gain._python_analysis_stack", lambda: object())
    def fail_python(*_args, **_kwargs):
        cancelled.set()
        raise ReplayGainAnalysisError("unsupported")
    monkeypatch.setattr("core.replay_gain._analyse_track_with_python", fail_python)
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_ffmpeg",
        lambda *_a, **_k: pytest.fail("cancelled fallback must not start"),
    )
    with pytest.raises(ReplayGainAnalysisCancelled):
        analyse_track(path, cancel_event=cancelled)


def test_both_decoders_fail_reports_both_causes_and_missing_file_never_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "both fail.m4a"
    path.write_bytes(b"fixture")
    monkeypatch.setattr("core.replay_gain._python_analysis_stack", lambda: object())
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_python",
        lambda *_a, **_k: (_ for _ in ()).throw(ReplayGainAnalysisError("python cause")),
    )
    calls = []
    monkeypatch.setattr(
        "core.replay_gain._analyse_track_with_ffmpeg",
        lambda *_a, **_k: (calls.append(True), (_ for _ in ()).throw(ReplayGainAnalysisError("ffmpeg cause")))[1],
    )
    with pytest.raises(ReplayGainAnalysisError, match="Python:.*python cause.*FFmpeg:.*ffmpeg cause"):
        analyse_track(path)
    assert calls == [True]

    calls.clear()
    with pytest.raises(ReplayGainAnalysisError, match="file not found"):
        analyse_track(tmp_path / "missing.m4a")
    assert calls == []


def test_album_ffmpeg_cancellation_terminates_and_awaits_child(tmp_path, monkeypatch):
    first, second = tmp_path / "one.wav", tmp_path / "two.wav"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    captured = {}

    class Process:
        returncode = None

        def __init__(self):
            self.terminated = False
            self.killed = False
            self.wait_calls = []

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 1

        def kill(self):
            self.killed = True
            self.returncode = 1

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            return self.returncode

    process = Process()
    monkeypatch.setattr("utils.paths.get_ffmpeg_executable", lambda: r"C:\bundle\ffmpeg.exe")
    monkeypatch.setattr(
        "core.replay_gain.subprocess.Popen",
        lambda command, **kwargs: (
            captured.update(command=command, kwargs=kwargs), process
        )[1],
    )

    class CancelAfterStart(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    with pytest.raises(ReplayGainAnalysisCancelled):
        analyse_album_program([first, second], cancel_event=CancelAfterStart())
    assert captured["command"].count("-i") == 2
    assert "concat=n=2:v=0:a=1" in " ".join(captured["command"])
    assert captured["kwargs"]["shell"] is False
    assert process.terminated and not process.killed
    assert process.wait_calls == [2]


def test_real_album_ffmpeg_child_is_reaped_on_cancellation(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    path = tmp_path / "real cancellation.wav"
    rate, seconds = 48000, 30
    with wave.open(str(path), "wb") as target:
        target.setnchannels(2)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\0\0\0\0" * rate * seconds)

    import core.replay_gain as replay_gain
    real_popen = replay_gain.subprocess.Popen
    cancel = threading.Event()
    children = []

    def start_real_child(command, **kwargs):
        child = real_popen(command, **kwargs)
        children.append(child)
        cancel.set()  # after the real child exists, before the polling loop
        return child

    monkeypatch.setattr(replay_gain.subprocess, "Popen", start_real_child)
    with pytest.raises(ReplayGainAnalysisCancelled):
        analyse_album_program([path], cancel_event=cancel)
    assert len(children) == 1
    assert children[0].poll() is not None
