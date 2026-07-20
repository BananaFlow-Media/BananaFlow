"""
core/metadata_processor.py  –  Tag reading, writing, and filename utilities
============================================================================
Pure Python — zero Qt imports.  Uses mutagen (already in requirements).

Format support
--------------
  MP3   → mutagen.id3  (ID3 v2.3/2.4 frames)
  FLAC  → mutagen.flac (Vorbis comments)
  M4A   → mutagen.mp4  (iTunes atoms)

Follows the same dispatch pattern as core/replay_gain.py.
All public functions log errors and never raise to the caller.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.metadata_models import (
    AudioTrackItem,
    ArtworkValue,
    OriginalTags,
    ProposedTags,
    RestoreOutcome,
    RestoreStatus,
    ScanResult,
    TrackStatus,
    metadata_values_equal,
    LYRICS_FIELD,
    REPLAYGAIN_FIELDS,
)
from core.metadata_backend import CapabilityLevel, FORMAT_CAPABILITIES, METADATA_BACKEND
# Same pattern already used by core/downloader.py and
# utils/cookie_validator.py: ui.i18n is Qt-free at import time (all Qt
# imports inside it are deferred), so this stays a headless-safe import
# and lets the one user-visible string this module builds (the Tag
# Editor's "unsupported format" tooltip) follow the active UI language.
from ui.i18n import t

logger = logging.getLogger(__name__)

_SUPPORTED_EXTS: frozenset[str] = frozenset(
    ext for capability in FORMAT_CAPABILITIES.all() if capability.writable for ext in capability.extensions
)
_AUDIO_EXTS: frozenset[str] = frozenset({
    ".wav", ".wma", ".aac", ".ogg", ".opus", ".ape", ".m4b",
    ".mp4", ".aif", ".aiff", ".alac",
})


def is_audio_candidate(path: Path) -> bool:
    """Return whether ``path`` belongs in a Tag Editor workspace.

    The filesystem monitor uses the same admission boundary as an initial
    scan, so an unrelated file event can never manufacture an unsupported
    table row.
    """
    return Path(path).suffix.lower() in (_SUPPORTED_EXTS | _AUDIO_EXTS)

# Matches a leading track number like "01 -", "1.", "002_", "03) ", "04 "
_TRACK_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*[-–.)_\s]")
# Used to strip the leading number+separator from a filename stem
_STRIP_NUM_RE = re.compile(r"^\s*\d{1,3}\s*[-–.)_\s]\s*")


# ──────────────────────────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────────────────────────

def clean_filename_to_title(filename: str) -> str:
    """
    Convert a filename (with or without extension) to a clean song title.

    Examples:
      "01 - אור.mp3"      → "אור"
      "02. Track.flac"    → "Track"
      "03_Song_Name.m4a"  → "Song Name"
      "No_Number.mp3"     → "No Number"
    """
    stem = Path(filename).stem
    # Strip leading track number (e.g. "01 - ", "02. ", "003_")
    cleaned = _STRIP_NUM_RE.sub("", stem).strip()
    # Replace underscores and runs of dashes used as word separators
    cleaned = cleaned.replace("_", " ")
    # Collapse multiple spaces
    cleaned = " ".join(cleaned.split())
    return cleaned if cleaned else stem


def extract_track_number(filename: str) -> Optional[int]:
    """
    Extract the leading track number from a filename.

    "01 - Song.mp3" → 1
    "15 Track.flac" → 15
    "No Num.mp3"    → None
    """
    m = _TRACK_NUM_RE.match(Path(filename).name)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def send_to_recycle_bin(path: Path) -> None:
    """
    Delete a file or folder via the OS Recycle Bin, falling back to a
    permanent delete only if `send2trash` is unavailable.

    Shared by every delete path in the tag editor (duplicate cleanup, table
    row delete, tree context menu) so "delete" always means "recoverable"
    unless the optional dependency is truly missing.
    """
    try:
        import send2trash
        send2trash.send2trash(str(path))
    except ImportError:
        if os.name == "nt":
            _send_to_windows_recycle_bin(path)
            return
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
        else:
            path.unlink()


def _send_to_windows_recycle_bin(path: Path) -> None:
    """Send a path to the Windows Recycle Bin through the Shell API."""
    if os.name != "nt":
        raise OSError("Windows Recycle Bin is only available on Windows")

    import ctypes
    from ctypes import wintypes

    FO_DELETE = 0x0003
    FOF_SILENT = 0x0004
    FOF_NOCONFIRMATION = 0x0010
    FOF_ALLOWUNDO = 0x0040
    FOF_NOERRORUI = 0x0400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.USHORT),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = FO_DELETE
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise OSError(f"SHFileOperationW failed with code {result}")
    if operation.fAnyOperationsAborted:
        raise OSError("Recycle Bin operation was aborted")


# ──────────────────────────────────────────────────────────────────────────────
# Scanning
# ──────────────────────────────────────────────────────────────────────────────


def scan_folders(root: Path, recursive: bool = True) -> set[Path]:
    """Return folders that should be shown in the tag-editor tree."""
    folders: set[Path] = {root}
    pattern = "**/*" if recursive else "*"

    for path in root.glob(pattern):
        try:
            if path.is_dir():
                folders.add(path)
        except OSError:
            logger.warning("[MetadataProcessor] Could not inspect folder candidate: %s", path)

    return folders


def collect_scan_targets(
    root: Path,
    recursive: bool = True,
) -> tuple[list[Path], set[Path], int]:
    """Find audio-like files and folders in one filesystem pass."""
    files: list[Path] = []
    folders: set[Path] = {root}
    skipped = 0

    def add_file(path: Path) -> None:
        nonlocal skipped
        if is_audio_candidate(path):
            files.append(path)
        else:
            skipped += 1

    if recursive:
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort(key=str.casefold)
                filenames.sort(key=str.casefold)
                folder = Path(dirpath)
                folders.add(folder)
                for filename in filenames:
                    add_file(folder / filename)
        except OSError as exc:
            logger.warning("[MetadataProcessor] Could not scan %s: %s", root, exc)
    else:
        try:
            with os.scandir(root) as entries_iter:
                entries = sorted(entries_iter, key=lambda entry: entry.name.casefold())
            for entry in entries:
                try:
                    path = Path(entry.path)
                    if entry.is_dir():
                        folders.add(path)
                    elif entry.is_file():
                        add_file(path)
                except OSError:
                    logger.warning("[MetadataProcessor] Could not inspect: %s", entry.path)
        except OSError as exc:
            logger.warning("[MetadataProcessor] Could not scan %s: %s", root, exc)

    return files, folders, skipped


def build_track_item(file_path: Path) -> AudioTrackItem:
    """Read one file into an AudioTrackItem."""
    ext = file_path.suffix.lower()

    from core.change_sets import capture_file_identity
    baseline_identity = capture_file_identity(file_path)
    detection = METADATA_BACKEND.detect(file_path)
    if not detection.capability.writable:
        return AudioTrackItem(
            path=file_path,
            folder=file_path.parent,
            ext=ext,
            status=(TrackStatus.READ_ONLY if detection.capability.level == CapabilityLevel.READ_ONLY else TrackStatus.UNSUPPORTED),
            error_msg=t(detection.capability.message_key),
            format_id=detection.format_id,
            metadata_editable=False,
            baseline_identity=baseline_identity,
        )

    try:
        # Do not use the forgiving compatibility reader here: a malformed
        # WAV/Opus container must be visibly unsupported, not silently shown
        # as a writable file with empty tags.
        tags = METADATA_BACKEND.read_legacy(file_path)
        return AudioTrackItem(
            path=file_path,
            folder=file_path.parent,
            ext=ext,
            original=tags,
            format_id=detection.format_id,
            metadata_editable=True,
            baseline_identity=baseline_identity,
        )
    except Exception as exc:
        logger.error("[MetadataProcessor] Error reading %s: %s", file_path.name, exc)
        return AudioTrackItem(
            path=file_path,
            folder=file_path.parent,
            ext=ext,
            status=TrackStatus.UNSUPPORTED,
            error_msg=t("meta_unsupported_format_tooltip"),
            format_id=detection.format_id,
            metadata_editable=False,
            baseline_identity=baseline_identity,
        )


def _folder_ancestors_within(root: Path, folder: Path) -> set[Path]:
    folders: set[Path] = set()
    current = folder
    while True:
        folders.add(current)
        if current == root or current.parent == current:
            break
        current = current.parent
    return folders


def build_scan_result(
    root: Path,
    tracks: list[AudioTrackItem],
    skipped: int,
    folders: Optional[set[Path]] = None,
    recursive: bool = True,
) -> ScanResult:
    folder_set = set(folders or {root})
    for track in tracks:
        folder_set.update(_folder_ancestors_within(root, track.folder))
    result = ScanResult(root=root, tracks=tracks, skipped_count=skipped,
                        folder_set=folder_set, recursive=bool(recursive))
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Tag reading
# ──────────────────────────────────────────────────────────────────────────────

def read_tags(path: Path) -> OriginalTags:
    """Compatibility reader delegated to the canonical metadata backend."""
    try:
        return METADATA_BACKEND.read_legacy(path)
    except Exception as exc:
        logger.warning("[MetadataProcessor] read_tags failed on %s: %s", path.name, exc)
    return OriginalTags()


# ──────────────────────────────────────────────────────────────────────────────
# Tag writing
# ──────────────────────────────────────────────────────────────────────────────

def write_tags(path: Path, proposed: ProposedTags, original: OriginalTags) -> bool:
    """
    Write proposed tags to file *in place*.  Only fields that differ from
    original are touched — every other tag on the file is preserved
    (TE-SAFE-07). Returns True on success, False on any error (logged).

    This is the in-place writer used by the restore path. The forward Apply
    pipeline uses `atomic_write_tags` (temp-copy → verify → atomic replace).
    """
    changed = proposed.changed_fields(original)
    if not changed:
        return True  # nothing to do

    effective = proposed.effective_tags(original)
    ext = path.suffix.lower()

    try:
        _dispatch_write(path, effective, changed, ext)
        logger.info("[MetadataProcessor] Tagged: %s", path.name)
        return True
    except PermissionError:
        logger.error("[MetadataProcessor] Permission denied writing %s", path)
        return False
    except Exception as exc:
        logger.error("[MetadataProcessor] Write error on %s: %s", path.name, exc)
        return False


def _dispatch_write(path: Path, effective: OriginalTags, changed: set[str], ext: str) -> None:
    """Compatibility writer delegated to the canonical metadata backend."""
    METADATA_BACKEND.write_legacy(path, effective, changed)


# ──────────────────────────────────────────────────────────────────────────────
# Atomic per-file media write — temp-copy → verify → atomic replace (R-ATOMIC)
# ──────────────────────────────────────────────────────────────────────────────

class ApplyWriteError(Exception):
    """Raised by atomic_write_tags on a write/verify failure.

    `stage` is one of ApplyStage.WRITE / ApplyStage.VERIFY and `code` a
    stable ApplyErrorCode. The original file is guaranteed untouched.
    """

    def __init__(self, stage: str, code: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code


class VerifiedFields(list[str]):
    """List-compatible write result carrying the canonical verified readback."""

    def __init__(self, fields, readback: OriginalTags) -> None:
        super().__init__(fields)
        self.readback = readback


def _verify_temp(
    path: Path, ext: str, effective: OriginalTags, changed: set[str]
) -> OriginalTags:
    """
    Read the temp copy back and confirm it still parses and that every
    explicitly changed field equals its intended value. Raises ApplyWriteError
    (stage=verify) on any mismatch or parse failure.
    """
    # One strict canonical read both reparses the detected container and
    # returns every field needed for semantic verification. The older path
    # opened each container twice, adding avoidable lock time on Windows.
    try:
        after = METADATA_BACKEND.read_legacy(path)
    except Exception as exc:
        raise ApplyWriteError(
            "verify", "verify_failed", f"container no longer parses: {exc}"
        ) from exc
    for name in changed:
        format_id = str(after.file_properties.get("format_id", ""))
        if not METADATA_BACKEND.values_equal(
            format_id, name, after.field_value(name), effective.field_value(name)
        ):
            raise ApplyWriteError(
                "verify", "verify_failed", f"field {name!r} did not verify"
            )
    return after


def atomic_write_tags(path: Path, proposed: ProposedTags, original: OriginalTags) -> list[str]:
    """
    Write only the proposed delta to a temp copy on the same filesystem,
    read back and verify every changed field (and that the container still
    parses), and *only then* atomically replace the original (R-ATOMIC /
    TE-SAFE-08/12).

    Returns the list of fields written. Raises ApplyWriteError on any
    write/verify failure — the original is left byte-for-byte untouched and
    the temp copy is removed.
    """
    import shutil
    import stat as _stat
    import tempfile

    changed = proposed.changed_fields(original)
    if not changed:
        return []

    effective = proposed.effective_tags(original)
    ext = path.suffix.lower()
    if ext not in _SUPPORTED_EXTS:
        raise ApplyWriteError(
            "write", "unsupported", f"Unsupported format for writing: {ext}"
        )

    # Capture the original file mode so we can restore it exactly on the temp
    # copy before the atomic replace — the mutation must not silently make a
    # read-only file writable (TE-SAFE / defect 4).
    try:
        orig_mode = _stat.S_IMODE(os.stat(str(path)).st_mode)
    except OSError:
        orig_mode = None

    # A read-only original needs extra handling (dest read-only clear so the
    # replace can proceed); the common writable case skips that.
    orig_read_only = orig_mode is not None and not (orig_mode & _stat.S_IWRITE)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".bananaflow_tmp_", suffix=ext
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # 1. Copy bytes (mtime is intentionally not preserved — the tag write is
        #    a modification, matching the prior in-place writer). The mkstemp
        #    temp is already writable and copyfile does not change its mode, so
        #    the mutation can proceed without an extra chmod.
        shutil.copyfile(str(path), str(tmp))

        # 2-3. Write only the delta to the temp copy; handles are closed by
        #      mutagen's save().
        try:
            _dispatch_write(tmp, effective, changed, ext)
        except Exception as exc:
            raise ApplyWriteError("write", "write_failed", str(exc)) from exc

        # 4. Read back and verify each explicitly changed field + reparse.
        verified_readback = _verify_temp(tmp, ext, effective, changed)

        # 5. Restore the ORIGINAL permission bits on the temp before the replace
        #    so the resulting file keeps the original's mode exactly — for a
        #    writable 0644 original as well as a read-only one (High correction).
        if orig_mode is not None:
            try:
                os.chmod(str(tmp), orig_mode)
            except OSError:
                logger.warning("[MetadataProcessor] Could not restore mode on %s", path.name)

        # 6. Flush temp copy to disk before replacing the original.
        try:
            with open(str(tmp), "rb") as fh:
                os.fsync(fh.fileno())
        except OSError:
            pass  # fsync unsupported on some filesystems — best effort

        # 7. Verified — atomically replace the original. On Windows a read-only
        #    destination cannot be replaced, so clear its read-only bit first;
        #    the temp carries the restored read-only mode, so the result keeps
        #    the original's permissions. If the replace fails, restore the
        #    original's mode before propagating so a read-only original stays
        #    byte-identical AND read-only.
        dest_mode_cleared = False
        if orig_read_only:
            try:
                os.chmod(str(path), orig_mode | _stat.S_IWRITE)
                dest_mode_cleared = True
            except OSError:
                pass
        try:
            os.replace(str(tmp), str(path))
        except OSError as exc:
            if dest_mode_cleared and orig_mode is not None:
                try:
                    os.chmod(str(path), orig_mode)
                except OSError:
                    pass
            raise ApplyWriteError(
                "write", "write_failed", f"atomic replace failed: {exc}") from exc
        logger.info("[MetadataProcessor] Atomically tagged: %s", path.name)
        return VerifiedFields(sorted(changed), verified_readback)

    except ApplyWriteError:
        _safe_unlink(tmp)
        raise
    except Exception as exc:
        _safe_unlink(tmp)
        raise ApplyWriteError("write", "write_failed", str(exc)) from exc


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            # A temp copy may carry a restored read-only mode; clear it so the
            # unlink succeeds on Windows (where read-only files can't be deleted).
            try:
                import stat as _stat
                os.chmod(str(path), _stat.S_IMODE(os.stat(str(path)).st_mode) | _stat.S_IWRITE)
            except OSError:
                pass
            path.unlink()
    except OSError:
        logger.warning("[MetadataProcessor] Could not remove temp file: %s", path)


# ──────────────────────────────────────────────────────────────────────────────
# Backup
# ──────────────────────────────────────────────────────────────────────────────

BACKUP_SCHEMA_VERSION = 4
_SUPPORTED_BACKUP_SCHEMAS = frozenset({2, 3, BACKUP_SCHEMA_VERSION})
# Probe threshold: refuse a backup target that can't hold at least this much.
_MIN_FREE_BYTES = 1 * 1024 * 1024   # 1 MiB — the JSON manifest is tiny


class BackupTargetError(Exception):
    """Backup destination is unusable (missing/uncreatable/unwritable/full).

    Raised by validate_backup_target() *before* any media is touched so the
    whole batch aborts with zero files modified (TE-SAFE-01).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BackupIntegrityError(ValueError):
    """A schema-3 backup cannot be trusted and must not be restored."""
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class _Schema4BackupRecord:
    """Tuple-compatible record carrying authorization evidence to Restore."""
    path: Path
    tags: OriginalTags
    original_identity: dict | None
    post_identity: dict | None
    require_verified_identity: bool

    def __iter__(self):
        yield self.path
        yield self.tags

    def __getitem__(self, index: int):
        return (self.path, self.tags)[index]


def validate_backup_target(backup_dir: Path) -> None:
    """
    Preflight the backup destination: exists/creatable, writable, and has
    free space. Raises BackupTargetError on any failure so the caller aborts
    the batch before writing a single media file (R-BACKUP).
    """
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupTargetError("invalid_dir", f"cannot create backup dir: {exc}") from exc

    if not backup_dir.is_dir():
        raise BackupTargetError("invalid_dir", f"not a directory: {backup_dir}")

    # Probe writability with a real temp file in the destination.
    import tempfile
    try:
        fd, probe = tempfile.mkstemp(dir=str(backup_dir), prefix=".bananaflow_probe_")
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise BackupTargetError("unwritable", f"backup dir not writable: {exc}") from exc

    # Probe free space where the platform reports it.
    try:
        usage = shutil.disk_usage(str(backup_dir))
        if usage.free < _MIN_FREE_BYTES:
            raise BackupTargetError(
                "disk_full", f"insufficient free space in {backup_dir}"
            )
    except BackupTargetError:
        raise
    except OSError:
        pass  # disk_usage unsupported — writability probe already passed


def _atomic_write_json(
    target: Path, payload: object, *, fsync: bool = True, validate: bool = True
) -> None:
    """
    temp (same dir) → flush → [fsync] → os.replace, optionally reparsing to
    validate first.

    The backup uses fsync+validate (a readback-validated, durable manifest —
    TE-SAFE-02). The journal, rewritten on every transition, uses the atomic
    temp→replace alone (which already guarantees crash-consistency against a
    process crash) and skips the readback+fsync to stay cheap.
    """
    import tempfile
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".bananaflow_json_")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        if validate:
            # Validate the temp is parseable JSON before it becomes the target.
            json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(str(tmp), str(target))
    except Exception:
        _safe_unlink(tmp)
        raise


def backup_tags(
    tracks: list[AudioTrackItem],
    backup_path: Path,
    *,
    operation_id: Optional[str] = None,
    root: Optional[Path] = None,
    app_version: str = "",
) -> str:
    """
    Write an atomic, readback-validated schema-4 operation manifest of all original
    tags (R-BACKUP / TE-SAFE-02/03). Returns the operation_id used.

    The write is temp → flush → fsync → os.replace → reopen-and-parse; any
    failure raises (leaving no partial destination) so the caller aborts the
    batch before touching media.
    """
    op_id = operation_id or uuid.uuid4().hex

    records = []
    for item in tracks:
        if item.original.artwork.read_state in {"invalid", "partial", "read_failed"}:
            raise BackupIntegrityError("artwork_invalid", "cannot back up invalid embedded artwork")
        original = item.original.to_dict()
        # A Phase-7 backup is authoritative even when the collection is empty.
        original["artwork_captured"] = True
        records.append({
            "original_path": str(item.path),
            "intended_path": str(item.path.parent / item.proposed_filename)
                              if item.proposed_filename else str(item.path),
            "final_path":    None,
            "identity":      _file_identity(item.path),
            "original":      original,
            "planned_fields": sorted(item.proposed.changed_fields(item.original)),
            "included":      not item.excluded_from_apply,
            "metadata_editable": item.metadata_editable,
            "result":        None,
        })

    from core.operation_manifest import build_operation_manifest, write_manifest
    # Older direct callers did not pass a selected scan root.  Their common
    # parent is still an operation-bound root; never leave schema-4 path
    # authorization without an authority boundary.
    operation_root = root
    if operation_root is None and tracks:
        operation_root = Path(os.path.commonpath([str(item.path.parent) for item in tracks]))
    payload = build_operation_manifest(
        operation_id=op_id, root=operation_root, app_version=app_version,
        operation_type="apply", records=records,
        created=datetime.now().isoformat(timespec="seconds"),
    )

    # Schema-4 manifests are self-authenticating before any loader returns a
    # record.  Use the manifest writer so the exact bytes are validated with
    # the canonical integrity envelope before publishing.
    write_manifest(backup_path, payload)
    logger.info(
        "[MetadataProcessor] Backup saved: %s (%d tracks, op %s)",
        backup_path.name, len(records), op_id[:8],
    )
    return op_id


def _file_identity(path: Path) -> Optional[dict]:
    """Best-effort {size, mtime_ns} identity so a restore can sanity-check."""
    try:
        st = path.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Restore from backup
# ──────────────────────────────────────────────────────────────────────────────

def load_tag_backup(backup_path: Path) -> list[tuple[Path, OriginalTags]]:
    """
    Parse a bananaflow_tag_backup_*.json file into (path, original-tags) pairs.

    Raises ValueError when the file is not a tag backup this app wrote
    (wrong JSON shape) — the caller shows that to the user before anything
    is touched on disk.
    """
    raw = json.loads(backup_path.read_text(encoding="utf-8"))

    # Schema 2/3 object form. Schema 3 additionally carries immutable artwork
    # payloads; schema 2 remains fully readable for old backups.
    # key is "original_path"; a completed rename is restored to final→original.
    if isinstance(raw, dict):
        if raw.get("schema") not in _SUPPORTED_BACKUP_SCHEMAS or not isinstance(raw.get("records"), list):
            raise ValueError(
                f"Not a BananaFlow tag backup (unrecognised object): {backup_path.name}"
            )
        if raw.get("schema") == 4:
            # Do this before resolving a single path.  A manifest is untrusted
            # input until its whole payload (paths, metadata, outcomes and
            # recovery evidence) has passed the schema-4 integrity check.
            from core.operation_manifest import validate_manifest
            validate_manifest(raw)
            return _load_schema4_records(raw, backup_path)
        if raw.get("schema") == 3:
            return _load_schema3_records(raw["records"], backup_path)
        return _load_schema2_records(raw["records"], backup_path)

    # Schema 1 (legacy top-level list): [{"path":..., "original":{...}}].
    if isinstance(raw, list):
        return _load_schema1_records(raw, backup_path)

    raise ValueError(f"Not a BananaFlow tag backup (expected a list or object): {backup_path.name}")


def _load_schema1_records(raw: list, backup_path: Path) -> list[tuple[Path, OriginalTags]]:
    known = {f.name for f in fields(OriginalTags)}
    records: list[tuple[Path, OriginalTags]] = []
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("original"), dict)
        ):
            raise ValueError(f"Not a BananaFlow tag backup (bad record shape): {backup_path.name}")
        tag_data = {k: v for k, v in entry["original"].items() if k in known}
        saved = OriginalTags.from_dict(tag_data); saved.artwork_captured = False
        records.append((Path(entry["path"]), saved))
    return records


def _load_schema2_records(raw: list, backup_path: Path) -> list[tuple[Path, OriginalTags]]:
    known = {f.name for f in fields(OriginalTags)}
    records: list[tuple[Path, OriginalTags]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("original"), dict):
            raise ValueError(f"Not a BananaFlow tag backup (bad record shape): {backup_path.name}")
        # A successfully-renamed file is restored to where it now lives; fall
        # back to the original path when the rename never completed.
        loc = entry.get("final_path") or entry.get("original_path")
        if not isinstance(loc, str):
            raise ValueError(f"Not a BananaFlow tag backup (bad record shape): {backup_path.name}")
        tag_data = {k: v for k, v in entry["original"].items() if k in known}
        saved = OriginalTags.from_dict(tag_data); saved.artwork_captured = False
        records.append((Path(loc), saved))
    return records


def _load_schema3_records(raw: list, backup_path: Path) -> list[tuple[Path, OriginalTags]]:
    known = {f.name for f in fields(OriginalTags)}
    records: list[tuple[Path, OriginalTags]] = []
    from core.artwork import ArtworkValidationError, validate_artwork_bytes
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("original"), dict):
            raise BackupIntegrityError("backup_invalid", backup_path.name)
        loc = entry.get("final_path") or entry.get("original_path")
        original = entry["original"]
        if not isinstance(loc, str) or original.get("artwork_captured") is not True or not isinstance(original.get("artwork"), dict):
            raise BackupIntegrityError("artwork_state_missing", backup_path.name)
        try:
            artwork = ArtworkValue.from_dict(original["artwork"], verify_integrity=True, require_integrity=True)
            for picture in artwork.entries:
                validate_artwork_bytes(picture.data, description=picture.description, picture_type=picture.picture_type)
        except (ValueError, ArtworkValidationError) as exc:
            raise BackupIntegrityError("artwork_integrity_failed", str(exc)) from exc
        tag_data = {k: v for k, v in original.items() if k in known}
        saved = OriginalTags.from_dict(tag_data)
        saved.artwork, saved.artwork_captured = artwork, True
        records.append((Path(loc), saved))
    return records


def _same_file_identity(path: Path, expected: object) -> bool:
    if not isinstance(expected, dict):
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    return (stat.st_size == expected.get("size")
            and stat.st_mtime_ns == expected.get("mtime_ns"))


def _within_operation_root(path: Path, root: object) -> bool:
    """Reject relative, alternate-root and traversal path authority."""
    if not isinstance(root, str) or not root:
        return False
    try:
        candidate = path.resolve(strict=False)
        allowed = Path(root).resolve(strict=False)
        candidate.relative_to(allowed)
    except (OSError, ValueError):
        return False
    return True


def _load_schema4_records(raw: dict, backup_path: Path) -> list[tuple[Path, OriginalTags]]:
    """Return only schema-4 targets independently authorized by its operation.

    Paths in a backup are hints.  A target must remain below the recorded root,
    be one of the operation's recorded locations, and match the recorded
    original or verified post-operation identity when it currently exists.
    Missing files remain previewable/restorable through the explicit resolution
    flow, but an accessible replacement is never accepted automatically.
    """
    records: list[tuple[Path, OriginalTags]] = []
    root = raw.get("root")
    known = {f.name for f in fields(OriginalTags)}
    from core.artwork import ArtworkValidationError, validate_artwork_bytes
    for entry in raw["records"]:
        original = entry["original"]
        if original.get("artwork_captured") is not True or not isinstance(original.get("artwork"), dict):
            raise BackupIntegrityError("artwork_state_missing", backup_path.name)
        try:
            artwork = ArtworkValue.from_dict(original["artwork"], verify_integrity=True, require_integrity=True)
            for picture in artwork.entries:
                validate_artwork_bytes(picture.data, description=picture.description, picture_type=picture.picture_type)
        except (ValueError, ArtworkValidationError) as exc:
            raise BackupIntegrityError("artwork_integrity_failed", str(exc)) from exc
        locations = [entry.get("final_path"), entry.get("intended_path"), entry.get("original_path")]
        allowed_locations = {value for value in locations if isinstance(value, str)}
        location = entry.get("final_path") or entry.get("original_path")
        if not isinstance(location, str) or location not in allowed_locations:
            raise BackupIntegrityError("path_unauthorized", backup_path.name)
        target = Path(location)
        if not target.is_absolute() or not _within_operation_root(target, root):
            raise BackupIntegrityError("path_unauthorized", str(target))
        tag_data = {key: value for key, value in original.items() if key in known}
        saved = OriginalTags.from_dict(tag_data)
        saved.artwork, saved.artwork_captured = artwork, True
        records.append(_Schema4BackupRecord(
            target, saved, entry.get("identity"), entry.get("expected_post_identity"),
            # A prepared backup has not yet produced a trusted post-operation
            # identity; it remains a historical/manual Restore backup.  Once
            # completed, only the verified operation-owned target is writable.
            raw.get("status") == "completed",
        ))
    return records


def restore_tags(
    records: list[tuple[Path, OriginalTags]],
    *,
    cancel_event=None,
    progress_cb=None,
) -> list[RestoreOutcome]:
    """
    Write the backed-up tags of each record back to its file.

    Nothing is deleted or renamed: only the nine tag fields captured by
    backup_tags() are rewritten, and only for files that still exist and
    whose current tags differ from the backup. Returns one RestoreOutcome
    per record so the UI can report exactly what happened to every file.
    """
    outcomes: list[RestoreOutcome] = []
    total = len(records)

    for i, record in enumerate(records):
        path, saved = record
        if cancel_event is not None and cancel_event.is_set():
            outcomes.extend(
                RestoreOutcome(path=p, status=RestoreStatus.CANCELLED, error="cancelled")
                for p, _ in records[i:]
            )
            break

        if not path.exists():
            outcomes.append(RestoreOutcome(path=path, status=RestoreStatus.MISSING))
        elif (isinstance(record, _Schema4BackupRecord) and record.require_verified_identity and not (
                _same_file_identity(path, record.post_identity)
                or _same_file_identity(path, record.original_identity))):
            # This is an authorization failure, not a normal per-file error:
            # no metadata writer has been invoked and the backup remains
            # available for explicit, reviewed user-assisted resolution.
            outcomes.append(RestoreOutcome(path=path, status=RestoreStatus.FAILED,
                                           error="file_identity_changed"))
        else:
            current = build_track_item(path)
            if current.status in (TrackStatus.UNSUPPORTED, TrackStatus.ERROR):
                outcomes.append(RestoreOutcome(
                    path=path, status=RestoreStatus.FAILED,
                    error=current.error_msg or "unreadable file",
                ))
            else:
                # ProposedTags convention: -1 clears the track number. Only
                # clear when the file currently has one — otherwise -1 vs
                # None would count as a change and force a pointless write.
                proposed = _restore_proposal(saved, current.original)
                if not proposed.has_changes(current.original):
                    outcomes.append(RestoreOutcome(path=path, status=RestoreStatus.UNCHANGED))
                else:
                    try:
                        atomic_write_tags(path, proposed, current.original)
                    except ApplyWriteError as exc:
                        outcomes.append(RestoreOutcome(
                            path=path, status=RestoreStatus.FAILED,
                            error=f"{exc.stage}:{exc.code}: {exc}",
                        ))
                    else:
                        outcomes.append(RestoreOutcome(path=path, status=RestoreStatus.RESTORED))

        if progress_cb is not None:
            progress_cb(i + 1, total)

    logger.info(
        "[MetadataProcessor] Restore finished: %d records, %d restored, %d failed",
        total,
        sum(1 for o in outcomes if o.status == RestoreStatus.RESTORED),
        sum(1 for o in outcomes if o.status == RestoreStatus.FAILED),
    )
    return outcomes


def _restore_proposal(saved: OriginalTags, current: OriginalTags) -> ProposedTags:
    """Build an explicit full-backup delta, including Phase 5/6 fields."""
    text_fields = (
        "title", "artist", "album", "album_artist", "comment", "year", "genre",
        "composer", "publisher", "copyright", "isrc", "grouping", "sort_title",
        "sort_artist", "sort_album", "sort_album_artist",
    )
    number_fields = ("track_num", "track_total", "disc_num", "disc_total", "bpm")
    proposed = ProposedTags()
    for name in text_fields:
        setattr(proposed, name, getattr(saved, name))
    for name in number_fields:
        saved_value = getattr(saved, name)
        current_value = getattr(current, name)
        setattr(proposed, name, -1 if saved_value is None and current_value is not None else saved_value)
    if not metadata_values_equal(LYRICS_FIELD, saved.lyrics, current.lyrics):
        if saved.lyrics.has_unsynchronized:
            from core.metadata_models import FieldChange, ChangeAction
            proposed.lyrics_change = FieldChange(ChangeAction.SET, saved.lyrics)
        else:
            proposed.clear_lyrics()
    if saved.artwork_captured and not metadata_values_equal("artwork", saved.artwork, current.artwork):
        # Schema-3 is authoritative: an explicitly empty collection restores
        # no artwork. Older schemas set artwork_captured=False and leave it.
        from core.metadata_models import FieldChange, ChangeAction
        target = saved.artwork if saved.artwork.entries else saved.artwork.without_all()
        proposed.artwork_change = FieldChange(ChangeAction.CLEAR if not target.entries else ChangeAction.SET, target)
    for field_name in REPLAYGAIN_FIELDS:
        saved_value = saved.replay_gain.field_value(field_name)
        current_value = current.replay_gain.field_value(field_name)
        if metadata_values_equal(field_name, saved_value, current_value):
            continue
        if saved_value is None:
            proposed.clear_replay_gain({field_name})
        else:
            proposed.set_replay_gain(field_name, saved_value)
    return proposed


# ──────────────────────────────────────────────────────────────────────────────
# Rename graph preflight (R-RENAME / TE-SAFE-04/05/10)
# ──────────────────────────────────────────────────────────────────────────────

from core.metadata_models import ApplyErrorCode  # noqa: E402  (grouped with helpers)

_RESERVED_STEMS: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_INVALID_NAME_CHARS = set('<>:"|?*')


import sys as _sys
import tempfile as _tempfile
from functools import lru_cache as _lru_cache

# Case-folding behavior is a property of the *filesystem*, not of the operating
# system, and the rename planner's collision detection depends on getting it
# right: guess "case-sensitive" on a folding filesystem and a case-only rename
# looks like a collision with a different file; guess the other way and two
# genuinely distinct files look like one.
#
# The platform constant below is only the last-resort answer. Windows and macOS
# usually fold (NTFS, APFS/HFS+) and Linux usually does not, but "usually" is
# doing real work in that sentence: a music library on an exFAT/NTFS external
# drive mounted under Linux folds, and macOS's APFS can be formatted
# case-sensitive. Deciding from `sys.platform` alone gets both of those wrong,
# in the one code path where being wrong means a rename is planned against the
# wrong collision model.
_PLATFORM_CASE_INSENSITIVE_DEFAULT = os.name == "nt" or _sys.platform == "darwin"


@_lru_cache(maxsize=512)
def _dir_is_case_insensitive(directory: str) -> bool:
    """Ask the filesystem holding ``directory`` whether it folds case.

    Creates a hidden, uniquely-named probe file ending in ``aA``, checks
    whether the otherwise-identical ``Aa`` spelling resolves to it, then
    removes it. Cached per directory, so this costs one create/stat/unlink per
    directory per process.

    Falls back to :data:`_PLATFORM_CASE_INSENSITIVE_DEFAULT` when the probe
    cannot run at all (read-only directory, permission denied, directory
    vanished) — an unwritable directory is one nothing is about to be renamed
    in anyway.
    """
    try:
        fd, probe_name = _tempfile.mkstemp(prefix=".bananaflow_case_", suffix="aA", dir=directory)
    except (OSError, ValueError):
        return _PLATFORM_CASE_INSENSITIVE_DEFAULT

    # Nothing between mkstemp and this try may fail: from the moment the file
    # exists, removing it has to be guaranteed. `os.close` belongs inside for
    # exactly that reason -- it can raise (EBADF/EINTR), and when it did so
    # above it left the probe file behind in the user's music folder forever,
    # with no path bound to unlink it by.
    probe = Path(probe_name)
    try:
        os.close(fd)
        # Same name, only the final two characters' case swapped. If the
        # filesystem folds case this resolves to the probe we just created.
        return probe.with_name(probe.name[:-2] + "Aa").exists()
    except OSError:
        return _PLATFORM_CASE_INSENSITIVE_DEFAULT
    finally:
        try:
            probe.unlink()
        except OSError:
            logger.warning("Could not remove case-probe file %s", probe)


def _rename_norm(path: Path) -> str:
    """Path key that folds case exactly when the containing filesystem does."""
    if _dir_is_case_insensitive(str(path.parent)):
        return str(path).casefold()
    return str(path)


def _same_file_ci(a: Path, b: Path) -> bool:
    return _rename_norm(a) == _rename_norm(b)


def _validate_rename_name(name: str) -> Optional[str]:
    """Return an ApplyErrorCode for an unsafe filename, else None."""
    if not name or name in (".", ".."):
        return ApplyErrorCode.RENAME_INVALID
    # A bare filename must never contain a path separator or parent ref.
    if "/" in name or "\\" in name or os.sep in name:
        return ApplyErrorCode.RENAME_ESCAPE
    if any(ch in _INVALID_NAME_CHARS for ch in name):
        return ApplyErrorCode.RENAME_INVALID
    if name != name.rstrip(" ."):
        return ApplyErrorCode.RENAME_INVALID  # trailing dot/space (Windows)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if stem.upper() in _RESERVED_STEMS:
        return ApplyErrorCode.RENAME_RESERVED
    return None


class RenameComponent:
    """
    One connected rename component (a single move, a chain, or a cycle) that
    must execute transactionally (R-RENAME / defect 3).

    component_id  stable id for the owner-aware ledger.
    members       original item keys (str(path)) whose renames belong here.
    steps         ordered (owner, src, dst) ops, including temp hops; `owner` is
                  the member key (original path str) of the *physical* file being
                  moved by that step, so the ledger tracks true file identity.
    final         {member_key: final_path} for the component's members.
    """

    def __init__(self, component_id: str = "") -> None:
        self.component_id = component_id
        self.members: set[str] = set()
        self.steps: list[tuple[str, Path, Path]] = []
        self.final: dict[str, Path] = {}


class RenamePlan:
    """
    The validated batch rename graph (R-RENAME).

    Attributes
    ----------
    components  list[RenameComponent] — the transactional units.
    steps       ordered [(src, dst)] across all components (back-compat view).
    blocked     {original_path_str: ApplyErrorCode} for hazardous renames whose
                proposal must be preserved and reported, never counted success.
    final       {original_path_str: final_path} across all components (view).
    """

    def __init__(self) -> None:
        self.components: list[RenameComponent] = []
        self.blocked: dict[str, str] = {}

    @property
    def steps(self) -> list[tuple[Path, Path]]:
        """Back-compat view: (src, dst) pairs across all components."""
        out: list[tuple[Path, Path]] = []
        for comp in self.components:
            out.extend((src, dst) for _owner, src, dst in comp.steps)
        return out

    @property
    def final(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for comp in self.components:
            out.update(comp.final)
        return out

    def component_for(self, member_key: str) -> Optional["RenameComponent"]:
        for comp in self.components:
            if member_key in comp.members:
                return comp
        return None


def _temp_rename_target(src: Path) -> Path:
    return src.parent / f".bananaflow_rn_{uuid.uuid4().hex[:12]}{src.suffix}"


def _order_component_steps(
    moves: dict[Path, Path], owner_of: dict[str, str]
) -> list[tuple[str, Path, Path]]:
    """
    Topologically order a set of moves, breaking cycles with temp hops, and
    attribute each step to the OWNER (original member key) of the physical file
    it moves. A temp hop inherits the owner of the file diverted into it, so the
    owner-aware ledger follows each physical file through the whole chain.
    """
    steps: list[tuple[str, Path, Path]] = []
    remaining = dict(moves)
    owner_at: dict[str, str] = {str(p): owner_of[str(p)] for p in moves}
    while remaining:
        progressed = False
        for src, dst in list(remaining.items()):
            if dst not in remaining:           # dst free (no pending move owns it)
                steps.append((owner_at[str(src)], src, dst))
                del remaining[src]
                progressed = True
        if not progressed:
            src, dst = next(iter(remaining.items()))
            temp = _temp_rename_target(src)
            owner = owner_at[str(src)]
            steps.append((owner, src, temp))
            owner_at[str(temp)] = owner       # temp inherits the file's owner
            del remaining[src]
            remaining[temp] = dst
    return steps


def plan_renames(items: list) -> RenamePlan:
    """
    Preflight the whole batch rename graph before any disk change and group it
    into transactional connected components.

    Each item is expected to expose `.path` (current) and `.proposed_filename`
    (target name, or None). Detects destination/case-insensitive collisions,
    case-only renames (temp hop), cycles (deterministic temp sequencing),
    reserved names, invalid chars, trailing dot/space and root escapes; any
    unresolved hazard blocks that single rename with its proposal preserved.
    """
    from collections import Counter

    plan = RenamePlan()

    requested: list[tuple[str, Path, Path]] = []   # (key, src, dst)
    for item in items:
        target = getattr(item, "proposed_filename", None)
        if not target or target == item.path.name:
            continue
        key = str(item.path)
        code = _validate_rename_name(target)
        if code is not None:
            plan.blocked[key] = code
            continue
        dst = item.path.parent / target
        requested.append((key, item.path, dst))

    if not requested:
        return plan

    # Duplicate-target (case-insensitive) collisions block every colliding item.
    target_counts = Counter(_rename_norm(dst) for _, _, dst in requested)

    # First pass: split out duplicate-target collisions and case-only renames;
    # the survivors form the real move graph.
    survivors: list[tuple[str, Path, Path]] = []
    case_only: list[tuple[str, Path, Path]] = []
    for key, src, dst in requested:
        if target_counts[_rename_norm(dst)] > 1:
            plan.blocked[key] = ApplyErrorCode.RENAME_COLLISION
        elif _same_file_ci(src, dst):
            case_only.append((key, src, dst))
        else:
            survivors.append((key, src, dst))

    # Sources that WILL actually move (so their targets are legitimately vacated).
    moving_srcs_ci = {_rename_norm(src) for _, src, _ in survivors}

    graph_moves: dict[Path, Path] = {}        # src -> dst
    move_key: dict[str, str] = {}             # norm(src) -> member key
    for key, src, dst in survivors:
        # External collision: dst already on disk and is NOT vacated by a move.
        if dst.exists() and _rename_norm(dst) not in moving_srcs_ci:
            plan.blocked[key] = ApplyErrorCode.RENAME_COLLISION
            continue
        graph_moves[src] = dst
        move_key[_rename_norm(src)] = key

    # Case-only renames are each their own single-member transactional component.
    for i, (key, src, dst) in enumerate(case_only):
        comp = RenameComponent(component_id=f"case{i}")
        comp.members.add(key)
        comp.final[key] = dst
        temp = _temp_rename_target(src)
        comp.steps = [(key, src, temp), (key, temp, dst)]
        plan.components.append(comp)

    if not graph_moves:
        return plan

    # Union-find over move endpoints to group connected moves into components.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for src, dst in graph_moves.items():
        union(_rename_norm(src), _rename_norm(dst))

    groups: dict[str, dict[Path, Path]] = {}
    for src, dst in graph_moves.items():
        groups.setdefault(find(_rename_norm(src)), {})[src] = dst

    for ci, moves in enumerate(groups.values()):
        comp = RenameComponent(component_id=f"comp{ci}")
        owner_of: dict[str, str] = {}
        for src, dst in moves.items():
            key = move_key.get(_rename_norm(src))
            if key is not None:
                comp.members.add(key)
                comp.final[key] = dst
                owner_of[str(src)] = key
        comp.steps = _order_component_steps(moves, owner_of)
        plan.components.append(comp)

    return plan


# ──────────────────────────────────────────────────────────────────────────────
# Durable Apply operation journal (R-JOURNAL / TE-SAFE-11)
# ──────────────────────────────────────────────────────────────────────────────

JOURNAL_SCHEMA_VERSION = 1


def apply_journal_path(backup_path: Path, operation_id: str) -> Path:
    """Journal file lives beside the backup: bananaflow_tag_apply_<op>.journal.json."""
    return backup_path.parent / f"bananaflow_tag_apply_{operation_id}.journal.json"


def write_journal(journal_path: Path, data: dict, *, durable: bool = False) -> None:
    """
    (Re)write the journal atomically (temp → flush → [fsync] → replace).

    Atomic replace alone guarantees crash-consistency against a *process* crash.
    Safety-critical transitions pass ``durable=True`` to add flush+fsync, so the
    record survives an OS crash / power loss too. Durable transitions are:
      * the initial PLANNED plan (before any media modification);
      * BACKED_UP → APPLYING;
      * every successful rename / temp-hop path transition;
      * batch completion (DONE) and any recovery-required terminal state.
    Non-critical intermediate repaints stay cheap (atomic replace only).
    """
    _atomic_write_json(journal_path, data, fsync=durable, validate=False)


def read_journal(journal_path: Path) -> dict:
    return json.loads(journal_path.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# Owner-aware rename ledger (blocker: journal identity)
# ──────────────────────────────────────────────────────────────────────────────

def _ledger(journal: dict) -> list:
    return journal.setdefault("rename_ledger", [])


def _ledger_next_seq(journal: dict) -> int:
    return max((e.get("seq", 0) for e in _ledger(journal)), default=0) + 1


def _ledger_add(journal, owner, comp_id, src, dst, state, seq) -> dict:
    entry = {
        "owner": str(owner), "component_id": comp_id,
        "src": str(src), "dst": str(dst), "state": state, "seq": seq,
    }
    _ledger(journal).append(entry)
    return entry


def resolve_owner_current(journal: dict, file_record: dict):
    """
    Resolve where a single owner's PHYSICAL file currently lives, using ONLY
    that owner's ledger entries (never global edges) plus safe disk inspection
    for a crashed step. Returns:
      ("resolved", Path)      — the file's current on-disk location;
      ("unresolved", [a, b])  — ambiguous; recovery must not guess or write;
      ("missing", None)       — not found on disk.
    """
    from core.metadata_models import RenameLedgerState as _L

    owner = str(file_record.get("original_path", ""))
    entries = sorted(
        (e for e in journal.get("rename_ledger", []) if e.get("owner") == owner),
        key=lambda e: e.get("seq", 0),
    )
    current = owner
    for e in entries:
        st = e.get("state")
        src, dst = str(e.get("src")), str(e.get("dst"))
        if st == _L.COMPLETED:
            current = dst
        elif st == _L.ROLLED_BACK:
            current = src
        elif st == _L.UNRESOLVED:
            return ("unresolved", [src, dst])
        elif st == _L.INTENT:
            # Crash between INTENT persist and the completion record: decide by
            # inspecting the two exact paths — never guess.
            src_ex, dst_ex = Path(src).exists(), Path(dst).exists()
            if dst_ex and not src_ex:
                current = dst
            elif src_ex and not dst_ex:
                current = src
            else:
                return ("unresolved", [src, dst])

    if entries:
        return ("resolved", Path(current)) if Path(current).exists() else ("missing", None)

    # No ledger for this owner: a completed rename recorded via final_path, or a
    # no-rename file. Use the owner-specific final_path/original only.
    final = file_record.get("final_path")
    if final and Path(final).exists():
        return ("resolved", Path(final))
    if Path(owner).exists():
        return ("resolved", Path(owner))
    return ("missing", None)


def _rename_error_code(exc: OSError) -> str:
    import errno
    if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM):
        return ApplyErrorCode.RENAME_LOCKED
    if getattr(exc, "errno", None) == errno.EEXIST:
        return ApplyErrorCode.RENAME_COLLISION
    return ApplyErrorCode.RENAME_FAILED


def execute_rename_component_txn(journal: dict, comp, persist_durable) -> dict:
    """
    Execute one rename component transactionally using the owner-aware ledger.

    Contract: persist INTENT before each os.replace; persist COMPLETED after;
    on a post-step persist failure roll the component back while the step list
    is known; persist ROLLED_BACK; if rollback or its persist fails, persist/
    retain UNRESOLVED with both possible paths. `persist_durable()` must write
    the journal durably and raise on failure. Returns a result dict:
      {"status": "ok" | "rolled_back" | "unresolved", "failure": (kind, code)}.
    """
    from core.metadata_models import RenameLedgerState as _L

    completed: list[dict] = []      # entries whose move succeeded
    unexecuted: Optional[dict] = None   # the failing step whose move did NOT happen
    failure = None                  # (kind, code)

    for owner, src, dst in comp.steps:
        seq = _ledger_next_seq(journal)
        entry = _ledger_add(journal, owner, comp.component_id, src, dst, _L.INTENT, seq)
        try:
            persist_durable()               # durable INTENT before any disk move
        except Exception:
            unexecuted = entry
            failure = ("journal", ApplyErrorCode.JOURNAL_FAILED)
            break
        if Path(dst).exists():              # never overwrite an existing dst
            unexecuted = entry
            failure = ("collision", ApplyErrorCode.RENAME_COLLISION)
            break
        try:
            os.replace(str(src), str(dst))
        except OSError as exc:
            unexecuted = entry
            failure = ("oserror", _rename_error_code(exc))
            break
        entry["state"] = _L.COMPLETED
        completed.append(entry)
        try:
            persist_durable()               # durable COMPLETED + owner current path
        except Exception:
            failure = ("journal", ApplyErrorCode.JOURNAL_FAILED)
            break

    if failure is None:
        return {"status": "ok", "failure": None}

    # The failing step never moved a file → its INTENT is a no-op; mark it so a
    # later reconstruction does not treat a real collision as ambiguous.
    if unexecuted is not None:
        unexecuted["state"] = _L.ROLLED_BACK

    # Roll back completed steps in reverse to restore the pre-component state.
    rollback_failed = False
    for entry in reversed(completed):
        src, dst = entry["src"], entry["dst"]
        try:
            if Path(src).exists() and Path(dst).exists():
                rollback_failed = True
                entry["state"] = _L.UNRESOLVED
                continue
            if Path(dst).exists():
                os.replace(dst, src)
            entry["state"] = _L.ROLLED_BACK
        except OSError:
            rollback_failed = True
            entry["state"] = _L.UNRESOLVED

    try:
        persist_durable()
    except Exception:
        rollback_failed = True

    if rollback_failed:
        for entry in completed:
            if entry.get("state") != _L.ROLLED_BACK:
                entry["state"] = _L.UNRESOLVED
        try:
            persist_durable()
        except Exception:
            pass
        return {"status": "unresolved", "failure": failure}
    return {"status": "rolled_back", "failure": failure}


def find_incomplete_journals(backup_dir: Path) -> list[Path]:
    """Return journal files whose batch_state is not DONE (crash survivors)."""
    from core.metadata_models import JournalBatchState

    out: list[Path] = []
    try:
        candidates = sorted(backup_dir.glob("bananaflow_tag_*.journal.json"))
    except OSError:
        return out
    for jp in candidates:
        try:
            data = read_journal(jp)
        except Exception:
            # Malformed crash records are themselves recovery evidence.  The UI
            # must surface them for inspection instead of silently losing them.
            out.append(jp)
            continue
        if data.get("batch_state") not in {JournalBatchState.DONE, JournalBatchState.COMPLETED}:
            out.append(jp)
    return out


def inspect_recovery_journal(journal_path: Path) -> dict:
    """Classify an interrupted operation from durable records and current disk.

    This is deliberately read-only.  Journal claims are checked against the
    backup/manifest, owner-aware rename ledger, file identity, and current tag
    values.  Unknown or contradictory evidence is reported as a conflict; it is
    never converted into permission to repeat a physical operation.
    """
    base = {
        "journal_path": str(journal_path), "operation_type": "unknown",
        "operation_id": "", "created": "", "backup_path": None,
        "affected_files": 0, "total": 0, "incomplete": 0,
        "completed_stages": [], "pending_stages": [], "files": [],
        "backup_status": "unknown", "manifest_status": "unknown",
        "current_disk_state": "unknown", "recommended_action": "inspect",
        "discard_allowed": False, "malformed": False,
    }
    try:
        journal = read_journal(journal_path)
    except Exception as exc:
        base.update(malformed=True, current_disk_state="journal_unreadable", error=str(exc))
        return base
    if not isinstance(journal, dict) or not isinstance(journal.get("files", {}), dict):
        base.update(malformed=True, current_disk_state="journal_invalid", error="invalid journal shape")
        return base

    op_type = str(journal.get("operation_type") or "apply")
    files = journal.get("files", {})
    base.update(
        operation_type=op_type, operation_id=str(journal.get("operation_id") or ""),
        created=str(journal.get("created") or ""), backup_path=journal.get("backup_path"),
        affected_files=len(files), total=len(files),
    )

    backup_raw = None
    backup_path = journal.get("backup_path")
    if not backup_path:
        base["backup_status"] = "missing_reference"
    elif not Path(backup_path).exists():
        base["backup_status"] = "missing"
    else:
        try:
            backup_raw = json.loads(Path(backup_path).read_text(encoding="utf-8"))
            backup_id = backup_raw.get("operation_id") if isinstance(backup_raw, dict) else None
            expected_backup_id = (journal.get("operation_id") if op_type == "apply"
                                  else journal.get("source_operation_id"))
            if backup_id and expected_backup_id and backup_id != expected_backup_id:
                base["backup_status"] = "operation_mismatch"
            else:
                base["backup_status"] = "verified"
            schema = backup_raw.get("schema") if isinstance(backup_raw, dict) else 1
            base["manifest_status"] = f"schema_{schema}"
        except Exception as exc:
            base.update(backup_status="malformed", manifest_status="invalid", backup_error=str(exc))

    saved_by_path: dict[Path, OriginalTags] = {}
    if base["backup_status"] == "verified" and backup_path:
        try:
            saved_by_path = dict(load_tag_backup(Path(backup_path)))
        except Exception:
            pass
    terminal = {"complete", "verified", "skipped"}
    file_summaries: list[dict] = []
    for key, record in files.items():
        if not isinstance(record, dict):
            file_summaries.append({"path": str(key), "disk_state": "invalid_record", "uncertainty": "conflict"})
            continue
        original = Path(record.get("original_path") or key)
        if op_type in {"apply", "undo_applied_batch"}:
            resolution, current_value = resolve_owner_current(journal, record)
            current = current_value if resolution == "resolved" else None
        else:
            current = Path(record.get("current_path") or record.get("original_path") or key)
            resolution = "resolved" if current.exists() else "missing"
        identity_state = "unavailable"
        expected_identity = record.get("post_write_identity") or record.get("pre_identity")
        if current is not None and isinstance(expected_identity, dict):
            try:
                stat = current.stat()
                identity_state = ("match" if stat.st_size == expected_identity.get("size")
                                  and stat.st_mtime_ns == expected_identity.get("mtime_ns") else "changed")
            except OSError:
                identity_state = "missing"
        state = str(record.get("state") or "planned")
        metadata_state = "unknown"
        if current is not None and op_type in {"restore", "undo_applied_batch"}:
            saved = saved_by_path.get(Path(record.get("current_path") or key))
            if saved is not None:
                try:
                    loaded = build_track_item(current)
                    if (loaded.status not in {TrackStatus.UNSUPPORTED, TrackStatus.ERROR}
                            and not _restore_proposal(saved, loaded.original).has_changes(loaded.original)):
                        metadata_state = "verified_on_disk"
                        if identity_state == "changed":
                            identity_state = "changed_by_verified_write"
                except Exception:
                    pass
        uncertainty = ("conflict" if resolution == "unresolved"
                       or identity_state == "changed" else "none")
        pending = state not in terminal
        physical_state = "not_applicable"
        if op_type == "apply":
            intended = Path(record.get("intended_path") or original)
            physical_state = ("complete" if current is not None and str(current) == str(intended)
                              else "pending" if str(intended) != str(original) else "not_applicable")
            pending = state not in {"complete", "skipped"}
            if state == "verified" and physical_state != "pending":
                pending = False
        elif op_type == "undo_applied_batch":
            target = Path(record.get("original_target") or original)
            physical_state = ("complete" if current is not None and str(current) == str(target)
                              else "pending" if str(target) != str(original) else "not_applicable")
            pending = state not in {"complete", "skipped"}
            if state == "verified" and physical_state != "pending":
                pending = False
        file_summaries.append({
            "path": str(original), "current_path": str(current) if current else None,
            "journal_state": state, "disk_state": resolution,
            "identity_state": identity_state, "metadata_state": metadata_state,
            "uncertainty": uncertainty,
            "physical_state": physical_state, "pending": pending,
        })

    base["files"] = file_summaries
    base["incomplete"] = sum(bool(item.get("pending")) for item in file_summaries)
    conflicts = [item for item in file_summaries if item.get("uncertainty") == "conflict"]
    missing = [item for item in file_summaries if item.get("disk_state") == "missing"]
    base["current_disk_state"] = ("conflicted" if conflicts else "missing_files" if missing else "evidence_consistent")

    state = str(journal.get("batch_state") or "preparing")
    stages = ["backup", "metadata", "physical", "reconciliation"]
    completed: list[str] = []
    if base["backup_status"] == "verified": completed.append("backup")
    if state in {"metadata_verified", "physical_preparing", "physical_complete", "reconciliation_pending"}:
        completed.append("metadata")
    if state in {"physical_complete", "reconciliation_pending"}: completed.append("physical")
    base["completed_stages"] = completed
    base["pending_stages"] = [stage for stage in stages if stage not in completed]

    no_writes = all(item.get("journal_state") in {"planned", "backed_up"} for item in file_summaries)
    if no_writes and state == "failed" and journal.get("failure_stage") == "backup":
        base.update(recommended_action="discard_obsolete", discard_allowed=True)
    elif conflicts or base["backup_status"] not in {"verified"}:
        base["recommended_action"] = "inspect"
    elif state == "reconciliation_pending" and not base["incomplete"]:
        base.update(recommended_action="reconcile", discard_allowed=True)
    elif not base["incomplete"] and not conflicts:
        base.update(recommended_action="reconcile", discard_allowed=True)
    elif op_type in {"restore", "undo_applied_batch"}:
        base["recommended_action"] = "resume_verified_pending"
    else:
        base["recommended_action"] = "restore_verified_backup"
    return base


def summarize_recovery(journal: dict) -> dict:
    """
    Build a review-first recovery summary from an incomplete journal.

    Never takes a destructive action — only describes what recovery would do
    so the panel can prompt the user (R-JOURNAL). The safe recovery path is a
    restore from the recorded backup, which reconstructs the original tags and
    (via schema-2 final_path) maps a completed rename back to its origin.
    """
    from core.metadata_models import JournalFileState

    files = journal.get("files", {})
    incomplete = [
        f for f in files.values()
        if f.get("state") not in (
            JournalFileState.COMPLETE, JournalFileState.SKIPPED,
            JournalFileState.CANCELLED,
        )
    ]
    return {
        "operation_type": journal.get("operation_type", "apply"),
        "operation_id": journal.get("operation_id", ""),
        "backup_path": journal.get("backup_path"),
        "total": len(files),
        "incomplete": len(incomplete),
        "states": {k: v.get("state") for k, v in files.items()},
    }


def execute_restore_recovery(journal_path: Path, *, cancel_event=None):
    """Resume only evidence-proven pending Restore/Undo metadata records.

    If a crash occurred after a write but before its VERIFIED transition, the
    current tags are compared with the backup first and the record is reconciled
    without calling the writer again.  A changed pre-write identity is a hard
    per-file conflict.
    """
    from core.metadata_models import JournalFileState, RestoreStatus
    journal = read_journal(journal_path)
    if journal.get("operation_type") not in {"restore", "undo_applied_batch"} or not journal.get("backup_path"):
        raise RecoveryPreflightError("wrong_operation", "not a recoverable Restore/Undo journal")
    records = load_tag_backup(Path(journal["backup_path"]))
    try:
        source_raw = json.loads(Path(journal["backup_path"]).read_text(encoding="utf-8"))
    except Exception as exc:
        raise RecoveryPreflightError("corrupt_backup", str(exc)) from exc
    expected_source = journal.get("source_operation_id")
    if (expected_source and isinstance(source_raw, dict)
            and source_raw.get("operation_id") != expected_source):
        raise RecoveryPreflightError("operation_mismatch", "journal source manifest changed")
    outcomes: list[RestoreOutcome] = []
    for path, tags in records:
        entry = journal.get("files", {}).get(str(path), {})
        if entry.get("state") in {JournalFileState.VERIFIED, JournalFileState.COMPLETE}:
            continue
        if cancel_event is not None and cancel_event.is_set():
            outcomes.append(RestoreOutcome(path, RestoreStatus.CANCELLED, "cancelled"))
            continue
        if path.exists():
            current = build_track_item(path)
            if current.status not in {TrackStatus.UNSUPPORTED, TrackStatus.ERROR}:
                if not _restore_proposal(tags, current.original).has_changes(current.original):
                    entry["state"] = JournalFileState.VERIFIED
                    entry["reconciled_from_disk"] = True
                    write_journal(journal_path, journal, durable=True)
                    outcomes.append(RestoreOutcome(path, RestoreStatus.UNCHANGED))
                    continue
            expected = entry.get("pre_identity")
            if isinstance(expected, dict):
                stat = path.stat()
                if stat.st_size != expected.get("size") or stat.st_mtime_ns != expected.get("mtime_ns"):
                    entry["state"] = JournalFileState.UNRESOLVED
                    entry["detail"] = "file_identity_changed"
                    write_journal(journal_path, journal, durable=True)
                    outcomes.append(RestoreOutcome(path, RestoreStatus.FAILED, "file_identity_changed"))
                    continue
        entry["state"] = JournalFileState.WRITTEN
        write_journal(journal_path, journal, durable=True)
        restored_batch = restore_tags([(path, tags)], cancel_event=cancel_event)
        if not restored_batch:
            continue
        outcome = restored_batch[0]
        outcomes.append(outcome)
        state = JournalFileState.VERIFIED if outcome.status in {RestoreStatus.RESTORED, RestoreStatus.UNCHANGED} else JournalFileState.FAILED
        journal["files"].setdefault(str(outcome.path), {})["state"] = state
        write_journal(journal_path, journal, durable=True)
    all_ok = all(item.get("state") in {JournalFileState.VERIFIED, JournalFileState.COMPLETE}
                 for item in journal.get("files", {}).values())
    journal["batch_state"] = "completed" if all_ok else "partial"
    write_journal(journal_path, journal, durable=True)
    return outcomes, all_ok


class RecoveryPreflightError(Exception):
    """Recovery cannot safely proceed (invalid/missing backup, missing tag
    record, operation mismatch). Raised BEFORE any disk modification so a
    broken backup never turns into a falsely-successful recovery (blocker 4)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _backup_tags_by_original(backup_path: Optional[str]) -> dict[str, OriginalTags]:
    """Lenient map original_path → OriginalTags (review-only; never raises)."""
    try:
        return load_backup_for_recovery({"backup_path": backup_path}, strict=False)
    except RecoveryPreflightError:
        return {}


def load_backup_for_recovery(journal: dict, *, strict: bool = True) -> dict[str, OriginalTags]:
    """
    Parse and VALIDATE the backup referenced by a journal, returning a map
    original_path → OriginalTags. In strict mode (blocker 4) a missing, corrupt,
    or wrong-schema backup, an operation-id mismatch, or a missing tag record
    for any file whose Apply changed tags raises RecoveryPreflightError before
    any disk change. In lenient mode it returns whatever it can parse.
    """
    backup_path = journal.get("backup_path")
    if not backup_path:
        if strict:
            raise RecoveryPreflightError("missing_backup", "no backup recorded")
        return {}
    bp = Path(backup_path)
    if not bp.exists():
        if strict:
            raise RecoveryPreflightError("missing_backup", f"backup not found: {bp}")
        return {}
    try:
        raw = json.loads(bp.read_text(encoding="utf-8"))
    except Exception as exc:
        if strict:
            raise RecoveryPreflightError("corrupt_backup", f"unreadable backup: {exc}")
        return {}

    known = {f.name for f in fields(OriginalTags)}
    if isinstance(raw, dict) and raw.get("schema") in _SUPPORTED_BACKUP_SCHEMAS:
        if raw.get("schema") == 4:
            # Recovery is also a write path: never let it bypass the sealed
            # manifest and detached operation binding used by manual Restore.
            from core.operation_manifest import validate_manifest, read_manifest
            validate_manifest(raw)
            read_manifest(bp)
        if strict:
            # Journal crash recovery requires a schema-2 backup whose non-empty
            # operation_id exactly equals the journal's (blocker: backup
            # identity). Legacy lists are only for the manual Restore workflow.
            jid = journal.get("operation_id")
            bid = raw.get("operation_id")
            if not bid or not jid:
                raise RecoveryPreflightError(
                    "operation_mismatch", "backup or journal operation id is missing")
            if jid != bid:
                raise RecoveryPreflightError(
                    "operation_mismatch",
                    f"backup operation {bid} != journal operation {jid}")
        records = raw.get("records", [])
    elif isinstance(raw, list):
        if strict:
            # A legacy top-level list has no operation id — not valid for crash
            # recovery (only for the manual Restore-from-Backup workflow).
            raise RecoveryPreflightError(
                "wrong_schema", "journal recovery requires a schema-2 backup")
        records = [{"original_path": r.get("path"), "original": r.get("original")}
                   for r in raw if isinstance(r, dict)]
    else:
        if strict:
            raise RecoveryPreflightError("wrong_schema", "unrecognised backup shape")
        return {}

    out: dict[str, OriginalTags] = {}
    for rec in records:
        op = rec.get("original_path")
        data = rec.get("original")
        if isinstance(op, str) and isinstance(data, dict):
            out[op] = OriginalTags.from_dict({k: v for k, v in data.items() if k in known})

    if strict:
        # Every file whose Apply changed TAGS must have a restorable record.
        for key, frec in journal.get("files", {}).items():
            if frec.get("changed_fields"):
                op = frec.get("original_path", key)
                if op not in out:
                    raise RecoveryPreflightError(
                        "missing_record",
                        f"no backup tag record for {op}")
    return out


def _reconstruct_current_path(rec: dict, journal: dict) -> Optional[Path]:
    """Owner-aware current location (Path) or None if unresolved/missing."""
    status, val = resolve_owner_current(journal, rec)
    return val if status == "resolved" else None


def _build_recovery_items(journal: dict, tags_by_original: dict) -> list[dict]:
    """Per-file recovery items with OWNER-AWARE current path + target tags.

    Identity is resolved per owner from that owner's ledger only (never global
    edges); an unresolved owner is never guessed and never tag-written.
    """
    items: list[dict] = []
    for key, rec in journal.get("files", {}).items():
        original_path = Path(rec.get("original_path", key))
        status, val = resolve_owner_current(journal, rec)
        current = val if status == "resolved" else None
        # Exact-string compare so a case-only rename (same file, different case)
        # is still recognised and its exact original casing is restored.
        needs_rename = current is not None and str(current) != str(original_path)
        items.append({
            "key": key,
            "record": rec,
            "original_path": original_path,
            "current_path": current,
            "resolution": status,           # resolved | unresolved | missing
            "needs_rename": needs_rename,
            "tags": tags_by_original.get(str(original_path)),
            "changed_fields": rec.get("changed_fields") or [],
            "state": rec.get("state"),
        })
    return items


class _RecoveryMoveShim:
    """Minimal item exposing .path / .proposed_filename for plan_renames."""

    def __init__(self, current: Path, target_name: str) -> None:
        self.path = current
        self.proposed_filename = target_name


def plan_recovery(journal_path: Path) -> dict:
    """
    Build a review-first recovery plan combining the journal (authoritative
    current-path reconstruction) and the backup (original tags). Also builds the
    transactional rename-back graph (components/chains/cycles/case-only) so a
    successful Apply swap can be undone. No disk change is made here.
    """
    journal = read_journal(journal_path)
    tags_by_original = _backup_tags_by_original(journal.get("backup_path"))
    items = _build_recovery_items(journal, tags_by_original)

    # Rename-back graph over current → original moves (reuses the Apply planner).
    shims = [
        _RecoveryMoveShim(it["current_path"], it["original_path"].name)
        for it in items if it["needs_rename"] and it["current_path"] is not None
    ]
    rename_plan = plan_renames(shims)
    collisions = sum(1 for c in rename_plan.blocked.values()
                     if c == ApplyErrorCode.RENAME_COLLISION)

    return {
        "operation_id": journal.get("operation_id", ""),
        "journal_path": str(journal_path),
        "backup_path": journal.get("backup_path"),
        "items": items,
        "rename_plan": rename_plan,
        "collisions": collisions,
    }


def _restore_tags_for(path: Path, saved: OriginalTags) -> RestoreOutcome:
    """
    Write `saved` tags back onto `path` using the SAME atomic model as Apply
    (temp-copy → delta-write → verify → atomic replace). A failure leaves the
    file byte-identical (additional correction).
    """
    current = build_track_item(path)
    if current.status in (TrackStatus.UNSUPPORTED, TrackStatus.ERROR):
        return RestoreOutcome(path=path, status=RestoreStatus.FAILED,
                              error=current.error_msg or "unreadable file")
    proposed = _restore_proposal(saved, current.original)
    try:
        fields_written = atomic_write_tags(path, proposed, current.original)
    except ApplyWriteError as exc:
        return RestoreOutcome(path=path, status=RestoreStatus.FAILED, error=str(exc))
    if fields_written:
        return RestoreOutcome(path=path, status=RestoreStatus.RESTORED)
    return RestoreOutcome(path=path, status=RestoreStatus.UNCHANGED)


def execute_recovery(
    journal_path: Path,
    *,
    cancel_event=None,
) -> tuple[list[RestoreOutcome], bool]:
    """
    Execute recovery (option 1): transactionally rename each file back to its
    original path (components/chains/cycles/case-only via temp hops, never
    overwriting an unrelated file), then atomically restore original tags.

    The rename-back reuses the SAME owner-aware ledger as Apply — recovery moves
    are attributed to the ORIGINAL owner and appended to the same journal ledger,
    so a crash mid-recovery is reconstructable and Recovery is retryable.

    Blocker 4: the backup is fully validated FIRST (raises RecoveryPreflightError
    before any disk change). Cancellation is honoured between the (atomic) rename
    phase and each tag write. Returns (outcomes, all_ok); the caller retires the
    journal only when all_ok is True.
    """
    journal = read_journal(journal_path)

    # ── Preflight: validate the backup BEFORE touching any path (blocker 4).
    tags_by_original = load_backup_for_recovery(journal, strict=True)
    items = _build_recovery_items(journal, tags_by_original)
    by_current = {str(it["current_path"]): it for it in items
                  if it["current_path"] is not None}

    outcomes: list[RestoreOutcome] = []
    all_ok = True
    failed_paths: set[str] = set()      # original-path strings already failed
    renamed_ok: set[str] = set()

    def _fail(op: Path, error: str) -> None:
        nonlocal all_ok
        outcomes.append(RestoreOutcome(path=op, status=RestoreStatus.FAILED, error=error))
        failed_paths.add(str(op))
        all_ok = False

    # ── Unresolved / missing owners are never guessed and never tag-written.
    for it in items:
        if it["resolution"] == "unresolved":
            _fail(it["original_path"], "current location unresolved (ambiguous)")
        elif it["resolution"] == "missing":
            outcomes.append(RestoreOutcome(path=it["original_path"],
                                           status=RestoreStatus.MISSING))
            all_ok = False
            failed_paths.add(str(it["original_path"]))

    # ── Build the transactional rename-back graph and remap step owners to the
    #    ORIGINAL path so the ledger tracks true physical identity across
    #    apply + recovery for retryability.
    # A resolved path is not sufficient evidence that it is still the same
    # operation-owned file.  Accept a recorded identity, the exact expected
    # post-Apply fields, or the already-restored backup fields.  Contradictory
    # evidence blocks both rename and metadata writes for that file.
    for it in items:
        current = it["current_path"]
        if current is None or str(it["original_path"]) in failed_paths:
            continue
        record = it["record"]
        expected_identity = record.get("post_write_identity")
        baseline_identity = record.get("baseline_identity")
        identity_matches = False
        for identity in (expected_identity, baseline_identity):
            if not isinstance(identity, dict):
                continue
            try:
                stat = current.stat()
                if stat.st_size == identity.get("size") and stat.st_mtime_ns == identity.get("mtime_ns"):
                    identity_matches = True
                    break
            except OSError:
                pass
        fields_to_check = record.get("changed_fields") or []
        metadata_matches = False
        backup_matches = False
        try:
            loaded = build_track_item(current)
            if loaded.status not in {TrackStatus.UNSUPPORTED, TrackStatus.ERROR}:
                expected_raw = record.get("expected_metadata")
                if isinstance(expected_raw, dict) and fields_to_check:
                    expected_tags = OriginalTags.from_dict(expected_raw)
                    metadata_matches = all(metadata_values_equal(
                        field_name, loaded.original.field_value(field_name),
                        expected_tags.field_value(field_name)) for field_name in fields_to_check)
                saved = it.get("tags")
                if saved is not None:
                    backup_matches = not _restore_proposal(
                        saved, loaded.original).has_changes(loaded.original)
        except Exception:
            pass
        has_evidence = (isinstance(expected_identity, dict)
                        or isinstance(baseline_identity, dict)
                        or bool(record.get("expected_metadata")))
        if has_evidence and not (identity_matches or metadata_matches or backup_matches):
            _fail(it["original_path"], "file_identity_changed")

    persist = lambda: write_journal(journal_path, journal, durable=True)  # noqa: E731
    shims = [
        _RecoveryMoveShim(it["current_path"], it["original_path"].name)
        for it in items
        if it["needs_rename"] and it["current_path"] is not None
        and str(it["original_path"]) not in failed_paths
    ]
    rename_plan = plan_renames(shims)

    # Blocked (external collision / invalid) rename-backs: fail, never overwrite.
    for cur_key, code in rename_plan.blocked.items():
        it = by_current.get(cur_key)
        if it is not None:
            _fail(it["original_path"], f"rename-back blocked ({code})")

    # The rename-back phase runs to completion (each component is atomic and is
    # what restores physical identity to the original paths); cancellation is
    # honoured *before the tag writes* below, keeping the journal for retry.
    for comp in rename_plan.components:
        # Remap each step's owner (currently the shim's current-path key) to the
        # owning original path, so recovery ledger entries chain to apply ones.
        comp.component_id = "rec_" + comp.component_id
        remapped = []
        for owner, src, dst in comp.steps:
            it = by_current.get(owner)
            new_owner = str(it["original_path"]) if it is not None else owner
            remapped.append((new_owner, src, dst))
        comp.steps = remapped

        result = execute_rename_component_txn(journal, comp, persist)
        for cur_key in comp.members:
            it = by_current.get(cur_key)
            if it is None:
                continue
            if result["status"] == "ok":
                renamed_ok.add(str(it["original_path"]))
            else:
                _fail(it["original_path"],
                      f"recovery rename component {result['status']}")

    # ── Restore tags atomically at each original path (cancellable boundary).
    for it in items:
        op: Path = it["original_path"]
        if str(op) in failed_paths:
            continue
        # Cancellation boundary: the current rename component already finished
        # or rolled back; stop before the next atomic tag write, keep journal.
        if cancel_event is not None and cancel_event.is_set():
            _fail(op, "cancelled")
            continue
        if not op.exists():
            outcomes.append(RestoreOutcome(path=op, status=RestoreStatus.MISSING))
            all_ok = False
            continue
        tags = it["tags"]
        if tags is None:
            # Only rename-only files reach here (tag-changed files are guaranteed
            # a record by preflight); the rename-back was the full recovery.
            outcomes.append(RestoreOutcome(path=op, status=RestoreStatus.RESTORED))
            continue
        result = _restore_tags_for(op, tags)
        if result.status == RestoreStatus.FAILED:
            all_ok = False
        elif result.status == RestoreStatus.UNCHANGED and str(op) in renamed_ok:
            result = RestoreOutcome(path=op, status=RestoreStatus.RESTORED)
        outcomes.append(result)

    return outcomes, all_ok
