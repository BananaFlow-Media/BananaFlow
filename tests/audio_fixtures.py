"""
tests/audio_fixtures.py  –  Minimal, dependency-free real audio containers
===========================================================================
Tag Editor / Converter tests need real MP3/FLAC/M4A files that mutagen
can genuinely open, tag, save, and re-read — not mocks — so a tag-write
bug (e.g. a field silently never being persisted) is actually caught.

Full audio fixtures would need FFmpeg or real encoded frames just to be
openable. These helpers write the smallest byte sequence each container
format requires to be structurally valid (FLAC's STREAMINFO block, a
minimal MP4 ftyp+moov+mdat atom tree, or nothing at all for MP3 — ID3
tags are a self-contained, prependable header mutagen can attach to any
file) with zero actual encoded audio. That's enough for every function
under test here (read_tags/write_tags/build_track_item/backup_tags/
restore_tags) since none of them decode audio — they only touch the
tag/metadata layer — while still exercising the real mutagen read/write
path instead of a mock of it. Verified against mutagen directly before
use here; not built from any format specification assumption alone.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path


def make_empty_mp3(path: Path) -> None:
    """A tag-only but structurally identifiable MP3 fixture.

    An empty ``.mp3`` cannot safely be granted writable capability from its
    filename alone.  A real ID3 header is enough for Mutagen's tag editor and
    mirrors a tag-only input without pretending arbitrary bytes are MP3.
    """
    from mutagen.id3 import ID3
    ID3().save(str(path))


def make_empty_flac(path: Path) -> None:
    """'fLaC' magic + one minimal-but-valid STREAMINFO metadata block
    (marked as the last block, so mutagen.flac.FLAC doesn't look for
    real audio frames after it)."""
    min_block = max_block = 4096
    sample_rate = 44100
    channels = 2
    bits_per_sample = 16

    bits = min_block
    bits = (bits << 16) | max_block
    bits = (bits << 24) | 0   # min frame size: unknown
    bits = (bits << 24) | 0   # max frame size: unknown
    bits = (bits << 20) | sample_rate
    bits = (bits << 3) | (channels - 1)
    bits = (bits << 5) | (bits_per_sample - 1)
    bits = (bits << 36) | 0   # total samples: unknown
    streaminfo = bits.to_bytes(18, "big") + bytes(16)  # + zeroed MD5
    assert len(streaminfo) == 34

    block_header = bytes([0x80]) + (34).to_bytes(3, "big")  # last-block, type 0
    path.write_bytes(b"fLaC" + block_header + streaminfo)


def make_empty_m4a(path: Path) -> None:
    """Minimal ftyp + moov(mvhd) + mdat atom tree. mutagen.mp4.MP4 walks
    the real box structure to find/create moov.udta.meta.ilst, so (unlike
    FLAC/MP3) a real, spec-correct mvhd box is required for it to open
    without raising MP4StreamInfoError."""

    def box(box_type: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + box_type + payload

    ftyp = box(b"ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")

    mvhd_payload = (
        struct.pack(">I", 0)              # version + flags
        + struct.pack(">I", 0)            # creation_time
        + struct.pack(">I", 0)            # modification_time
        + struct.pack(">I", 600)          # timescale
        + struct.pack(">I", 0)            # duration
        + struct.pack(">i", 0x00010000)   # rate, 1.0
        + struct.pack(">h", 0x0100)       # volume, 1.0
        + bytes(10)                       # reserved
        + bytes(36)                       # matrix
        + bytes(24)                       # pre_defined
        + struct.pack(">I", 1)            # next_track_ID
    )
    assert len(mvhd_payload) == 100
    moov = box(b"moov", box(b"mvhd", mvhd_payload))
    mdat = box(b"mdat", b"")

    path.write_bytes(ftyp + moov + mdat)


def make_empty_wav(path: Path) -> None:
    """A real PCM RIFF/WAVE container suitable for Mutagen's ID3-in-WAV API."""
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * 16)


MAKERS = {
    ".mp3": make_empty_mp3,
    ".flac": make_empty_flac,
    ".m4a": make_empty_m4a,
    ".wav": make_empty_wav,
}


def make_empty_audio(path: Path) -> None:
    """Dispatch on path.suffix to the matching make_empty_* helper."""
    MAKERS[path.suffix.lower()](path)
