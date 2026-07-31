"""
ui/workers/duplicate_detector_worker.py  –  Background duplicate audio file detector
======================================================================================
Scans a folder for duplicate audio files in two passes, at any library size:

  pass 1 → group by AUDIO PAYLOAD LENGTH   (header parsing only, no audio read)
  pass 2 → group by AUDIO-ONLY MD5 hash    (only files that shared a length)

Why the length pass is exact, not a heuristic
---------------------------------------------
Identical audio bytes have an identical byte length, so every group the hash
pass would form has all its members in one length bucket already.  Pass 1 can
therefore never split a real duplicate group -- it only removes files that no
hash could have matched anyway.  The result is byte-for-byte the same as
hashing everything, for a fraction of the reads.

Note that the bucket key is the length of the *payload*, not of the file.
Grouping on file size would defeat the whole design below: two copies of one
song that differ only in their embedded cover art have different file sizes
and would never be compared.

A shared length between two different songs (same frame count on a CBR encode)
costs one wasted hash and can never produce a false group, because pass 2 still
has to agree.

Audio-only hashing
------------------
Files that share identical audio but differ only in their embedded cover art
(album art / APIC tag) are now correctly detected as duplicates.

The worker parses format-specific container headers to locate where the raw audio
stream begins and ends, then hashes ONLY those bytes — skipping ID3 tags, Vorbis
comments, cover-art blobs, and any other metadata:

  .mp3   — skips ID3v2 header at start of file (syncsafe size field);
            skips trailing ID3v1 tag (128-byte "TAG" footer) if present.
  .flac  — walks FLAC metadata-block chain ("fLaC" marker) and starts
            hashing from the first audio frame that follows.
  .m4a / .mp4
         — scans top-level atoms; hashes only the content of the
            "mdat" atom (raw compressed audio data).
  all other formats (.ogg, .wav, .opus, .wma …)
         — falls back to full-file hashing (metadata is a tiny fraction
            of these files' sizes, so false-negatives are rare).

Signals
-------
progress(int, int, str)      scanned_count, total_count, eta_string
finished(object, float, str) {key: [Path, …]}, elapsed_seconds, strategy_label
error(str)                   unrecoverable error message
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ui.i18n import t
from core.metadata_validation import (
    DuplicateConfidence, DuplicateEvidence, DuplicateGroup, DuplicateScanResult,
)

_AUDIO_EXTS = frozenset({
    ".mp3", ".flac", ".m4a", ".ogg",
    ".wav", ".aac", ".opus", ".wma", ".mp4",
})

# One strategy now, kept as a label because the result and its dialog still
# report how a group was matched.
_STRATEGY = "md5"


class DuplicateDetectorWorker(QThread):
    """
    QThread worker that finds duplicate audio files in a folder.

    Emits incremental progress with a live ETA, then emits the duplicate
    groups dict together with elapsed time and the strategy name used.
    """

    progress = Signal(int, int, str)      # done, total, eta_str
    finished = Signal(object, float, str) # groups dict, elapsed_sec, strategy
    error    = Signal(str)

    def __init__(self, folder: Path, recursive: bool, parent=None, *, request_id: int = 0, generation: int = 0) -> None:
        super().__init__(parent)
        self._folder    = folder
        self._recursive = recursive
        self._cancel    = threading.Event()
        self._request_id = request_id
        self._generation = generation
        self._warnings: list[tuple[str, str, str]] = []

    def cancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        t0 = time.perf_counter()
        try:
            pattern = "**/*" if self._recursive else "*"
            all_files = [
                p for p in self._folder.glob(pattern)
                if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
            ]

            total = len(all_files)
            if total == 0:
                self.finished.emit(DuplicateScanResult(generation=self._generation, request_id=self._request_id), time.perf_counter() - t0, _STRATEGY)
                return

            candidates = self._same_length_candidates(all_files, total, t0)
            groups = self._find_by_md5(candidates, len(candidates), time.perf_counter())

            if self._cancel.is_set():
                self.finished.emit(DuplicateScanResult(generation=self._generation, request_id=self._request_id, cancelled=True, warnings=tuple(self._warnings)),
                                   time.perf_counter() - t0, _STRATEGY)
                return
            self.finished.emit(self._structured(groups), time.perf_counter() - t0, _STRATEGY)

        except Exception as exc:
            self.error.emit(str(exc))

    def _structured(self, groups: dict) -> DuplicateScanResult:
        """Every group now comes from a hash, so every group is high confidence."""
        structured = []
        for key, members in groups.items():
            paths = tuple(sorted((Path(path) for path, _hash in members),
                                 key=lambda path: str(path).casefold()))
            # A group is only an audio-payload match if every member's container
            # actually parsed; one fallback to whole-file hashing downgrades the
            # claim for the whole group.
            evidence = (DuplicateEvidence.AUDIO_PAYLOAD
                        if all(value.evidence is DuplicateEvidence.AUDIO_PAYLOAD for _path, value in members)
                        else DuplicateEvidence.WHOLE_FILE)
            group_id = hashlib.sha256((f"{_STRATEGY}|{key}|" + "|".join(map(str, paths))).encode("utf-8")).hexdigest()[:24]
            structured.append(DuplicateGroup(group_id, tuple(map(str, paths)), evidence,
                                             DuplicateConfidence.HIGH, _STRATEGY, None))
        return DuplicateScanResult(tuple(structured), generation=self._generation, request_id=self._request_id,
                                   partial=bool(self._warnings), warnings=tuple(self._warnings))

    # ── Grouping strategies ────────────────────────────────────────────────────

    def _same_length_candidates(self, files: list[Path], total: int, t0: float) -> list[Path]:
        """Pass 1: keep only files whose audio payload length is not unique.

        Reads container headers, never audio.  A file whose header cannot be
        read is kept rather than dropped: letting it fall out here would lose
        a real duplicate silently, which is the one way this pass could ever
        cost accuracy.
        """
        from collections import defaultdict
        by_length: dict[int, list[Path]] = defaultdict(list)
        unreadable: list[Path] = []

        for i, f in enumerate(files):
            if self._cancel.is_set():
                return []
            try:
                by_length[self._payload_length(f)].append(f)
            except OSError:
                self._warnings.append((str(f), "prefilter", "duplicate_read_failed"))
                unreadable.append(f)
            if i % 200 == 0 or i == total - 1:
                self.progress.emit(i + 1, total, self._eta(i + 1, total, t0))

        return [path for group in by_length.values() if len(group) > 1
                for path in group] + unreadable

    def _payload_length(self, path: Path) -> int:
        """Byte length of the audio stream, from headers alone."""
        with open(path, "rb") as fp:
            start, end, _evidence = self._audio_bounds(fp, path.suffix.lower())
            if end is None:
                fp.seek(0, 2)
                end = fp.tell()
        return end - start

    def _find_by_md5(self, files: list[Path], total: int, t0: float) -> dict:
        from collections import defaultdict
        hash_map: dict[str, list[tuple[Path, object]]] = defaultdict(list)

        for i, f in enumerate(files):
            if self._cancel.is_set():
                return {}
            try:
                result = self._audio_hash(f)
                if result is not None:
                    hash_map[result.digest].append((f, result))
            except OSError:
                self._warnings.append((str(f), "hash", "duplicate_read_failed"))
            self.progress.emit(i + 1, total, self._eta(i + 1, total, t0))

        return {k: v for k, v in hash_map.items() if len(v) > 1}

    # ── Audio-only hashing ─────────────────────────────────────────────────────

    def _audio_hash(self, path: Path):
        """
        Hash only the raw audio stream bytes, ignoring embedded metadata and
        cover art so that files differing only in their album art are detected
        as duplicates.
        """
        h = hashlib.md5()

        with open(path, "rb") as fp:
            start, end, evidence = self._audio_bounds(fp, path.suffix.lower())

            fp.seek(start)
            remaining = (end - start) if end is not None else None

            while True:
                if self._cancel.is_set():
                    return None
                to_read = min(8192, remaining) if remaining is not None else 8192
                chunk   = fp.read(to_read)
                if not chunk:
                    break
                h.update(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
                    if remaining <= 0:
                        break

        from core.metadata_validation import DuplicateHash
        return DuplicateHash(h.hexdigest(), evidence)

    @staticmethod
    def _audio_bounds(fp, suffix: str) -> tuple[int, int | None, DuplicateEvidence]:
        """Where the audio stream starts and ends, and how sure we are.

        Both passes go through here, so the length a file is bucketed on and
        the bytes it is hashed over can never describe different ranges.
        ``end`` of None means "to end of file".
        """
        cls = DuplicateDetectorWorker
        if suffix == ".mp3":
            return (cls._mp3_audio_start(fp), cls._mp3_audio_end(fp),
                    DuplicateEvidence.AUDIO_PAYLOAD)
        if suffix == ".flac":
            start = cls._flac_audio_start(fp)
            # A malformed FLAC cannot prove where metadata ends.  Hashing the
            # complete bytes is safe, but must not be described as an
            # audio-payload comparison.
            if start is None:
                return 0, None, DuplicateEvidence.WHOLE_FILE
            return start, None, DuplicateEvidence.AUDIO_PAYLOAD
        if suffix in (".m4a", ".mp4"):
            bounds = cls._m4a_mdat_bounds(fp)
            if bounds is None:
                return 0, None, DuplicateEvidence.WHOLE_FILE
            return bounds[0], bounds[1], DuplicateEvidence.AUDIO_PAYLOAD
        # Fallback: the whole file, for containers with no parser here.
        return 0, None, DuplicateEvidence.WHOLE_FILE

    # ── Format-specific offset parsers ─────────────────────────────────────────

    @staticmethod
    def _mp3_audio_start(fp) -> int:
        """
        Return the byte offset where MP3 audio frames begin.

        ID3v2 tag sits at the very start of the file.  Its size is stored in
        bytes 6-9 as a 4-byte syncsafe integer (each byte contributes 7 bits,
        MSB is always 0).  The fixed 10-byte header is not included in this
        size field, so audio_start = 10 + syncsafe_size.
        """
        fp.seek(0)
        header = fp.read(10)
        if len(header) < 10 or header[:3] != b"ID3":
            return 0
        size = (
            (header[6] & 0x7F) << 21 |
            (header[7] & 0x7F) << 14 |
            (header[8] & 0x7F) << 7  |
            (header[9] & 0x7F)
        )
        return 10 + size

    @staticmethod
    def _mp3_audio_end(fp) -> int | None:
        """
        Return the byte offset of a trailing ID3v1 tag if one is present,
        or None if the audio runs to end-of-file.

        ID3v1 is always exactly 128 bytes, positioned at the very end of the
        file, and starts with the three ASCII bytes 'TAG'.
        """
        try:
            fp.seek(-128, 2)          # 128 bytes before EOF
            if fp.read(3) == b"TAG":
                fp.seek(0, 2)         # go to EOF to read total size
                return fp.tell() - 128
        except OSError:
            pass
        return None

    @staticmethod
    def _flac_audio_start(fp) -> int | None:
        """
        Return the byte offset where FLAC audio frame data begins.

        A FLAC file starts with the 4-byte marker "fLaC" followed by one or
        more METADATA_BLOCK structures.  Each block has a 4-byte header:
          - bit 7     : 1 if this is the last metadata block
          - bits 6-0  : block type (STREAMINFO=0, PICTURE=6, …)
          - bytes 1-3 : 24-bit block data length (big-endian)
        Audio frames immediately follow the final metadata block.
        """
        fp.seek(0)
        if fp.read(4) != b"fLaC":
            return None
        offset = 4
        while True:
            block_header = fp.read(4)
            if len(block_header) < 4:
                return None
            is_last = bool(block_header[0] & 0x80)
            blk_len = (block_header[1] << 16) | (block_header[2] << 8) | block_header[3]
            offset += 4 + blk_len
            fp.seek(0, 2)
            if offset > fp.tell():
                return None
            if is_last:
                return offset
            fp.seek(offset)

    @staticmethod
    def _m4a_mdat_bounds(fp) -> tuple[int, int] | None:
        """
        Scan top-level MP4/M4A atoms and return (content_start, content_end)
        for the first 'mdat' atom found (raw compressed audio data).

        Each atom begins with:
          - 4 bytes: atom size in bytes (including the 8-byte header)
          - 4 bytes: atom type (FourCC, e.g. b'ftyp', b'moov', b'mdat')
        The content of 'mdat' is the audio bitstream with no tags mixed in.
        """
        fp.seek(0)
        while True:
            size_bytes = fp.read(4)
            type_bytes = fp.read(4)
            if len(size_bytes) < 4 or len(type_bytes) < 4:
                return None
            atom_size = int.from_bytes(size_bytes, "big")
            if atom_size < 8:
                return None
            if type_bytes == b"mdat":
                content_start = fp.tell()                   # right after 8-byte header
                content_end   = content_start + atom_size - 8
                fp.seek(0, 2)
                return (content_start, content_end) if content_end <= fp.tell() else None
            fp.seek(atom_size - 8, 1)                       # skip to next atom

    # ── ETA helper ─────────────────────────────────────────────────────────────

    @staticmethod
    def _eta(done: int, total: int, t0: float) -> str:
        if done == 0:
            return t("dup_calculating")
        elapsed   = time.perf_counter() - t0
        remaining = (total - done) / (done / elapsed)
        if remaining < 60:
            return f"~{int(remaining)}s"
        mins, secs = divmod(int(remaining), 60)
        return f"~{mins}m{secs:02d}s"
