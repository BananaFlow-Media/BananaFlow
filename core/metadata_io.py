"""Qt-free Phase 12 metadata IO contracts and atomic publication helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import errno
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from typing import Callable, Iterable


class IOScope(str, Enum):
    SELECTED = "selected"
    VISIBLE = "visible"
    CHANGED = "changed"
    ALL_LOADED = "all_loaded"


class MetadataValueSource(str, Enum):
    ORIGINAL = "original"
    EFFECTIVE = "effective"


class IOErrorKind(str, Enum):
    CANCELLED = "cancelled"
    EMPTY_SCOPE = "empty_scope"
    SOURCE_MISSING = "source_missing"
    SOURCE_CHANGED = "source_changed"
    SOURCE_TOO_LARGE = "source_too_large"
    INVALID_ENCODING = "invalid_encoding"
    INVALID_FORMAT = "invalid_format"
    RESOURCE_LIMIT = "resource_limit"
    INVALID_MAPPING = "invalid_mapping"
    STALE_PREVIEW = "stale_preview"
    DESTINATION_EXISTS = "destination_exists"
    DESTINATION_INVALID = "destination_invalid"
    PERMISSION_DENIED = "permission_denied"
    WRITE_FAILED = "write_failed"
    READBACK_FAILED = "readback_failed"
    UNSUPPORTED_PUBLICATION = "unsupported_publication"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


class IOWarningKind(str, Enum):
    EMPTY_SCOPE = "empty_scope"
    STALE_IDENTITY = "stale_identity"
    PENDING_RENAME = "pending_rename"
    MISSING_FILE = "missing_file"
    ABSOLUTE_PATH = "absolute_path"
    PARTIAL = "partial"


_ERROR_KEYS = {kind: f"meta_io_error_{kind.value}" for kind in IOErrorKind}
_WARNING_KEYS = {kind: f"meta_io_warning_{kind.value}" for kind in IOWarningKind}


@dataclass(frozen=True)
class IOErrorInfo:
    kind: IOErrorKind
    message_key: str = ""
    arguments: tuple[tuple[str, object], ...] = ()
    line: int | None = None
    column: int | None = None

    def __post_init__(self) -> None:
        if not self.message_key:
            object.__setattr__(self, "message_key", _ERROR_KEYS[self.kind])


@dataclass(frozen=True)
class IOWarningInfo:
    kind: IOWarningKind
    message_key: str = ""
    arguments: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        if not self.message_key:
            object.__setattr__(self, "message_key", _WARNING_KEYS[self.kind])


class MetadataIOError(RuntimeError):
    def __init__(self, info: IOErrorInfo):
        super().__init__(info.kind.value)
        self.info = info


@dataclass(frozen=True)
class IORequestIdentity:
    request_id: str
    operation: str
    workspace_generation: int
    change_revision: int
    item_ids: tuple[int, ...]
    source_identity: "SourceFileIdentity | None" = None
    mapping_identity: str = ""
    content_revision: int = 0

    @classmethod
    def create(cls, operation: str, generation: int, revision: int,
               item_ids: Iterable[int], *, source_identity=None,
               mapping_identity: str = "", content_revision: int = 0) -> "IORequestIdentity":
        unique = tuple(dict.fromkeys(int(value) for value in item_ids))
        return cls(uuid.uuid4().hex, operation, generation, revision, unique,
                   source_identity, str(mapping_identity), int(content_revision))

    def current_for(self, workspace, *, exact_ids: Iterable[int] | None = None) -> bool:
        ids = self.item_ids if exact_ids is None else tuple(exact_ids)
        return (
            self.workspace_generation == workspace.generation
            and self.change_revision == workspace.change_set.revision
            and (not self.content_revision
                 or self.content_revision == workspace.content_revision)
            and ids == self.item_ids
            and all(workspace.track_for_id(identity) is not None for identity in self.item_ids)
        )


@dataclass(frozen=True)
class SourceFileIdentity:
    path: Path
    size: int
    modified_time_ns: int
    sha256: str

    @classmethod
    def capture(
        cls,
        path: Path,
        *,
        maximum_bytes: int | None = None,
        cancellation: "CancellationToken | None" = None,
        chunk_bytes: int = 64 * 1024,
    ) -> "SourceFileIdentity":
        if cancellation:
            cancellation.raise_if_cancelled()
        try:
            stat = path.stat()
        except OSError as exc:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.SOURCE_MISSING)) from exc
        if maximum_bytes is not None and stat.st_size > maximum_bytes:
            raise MetadataIOError(IOErrorInfo(
                IOErrorKind.SOURCE_TOO_LARGE,
                arguments=(("maximum", maximum_bytes), ("actual", stat.st_size)),
            ))
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(max(4096, int(chunk_bytes))):
                    if cancellation:
                        cancellation.raise_if_cancelled()
                    digest.update(chunk)
        except OSError as exc:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.SOURCE_MISSING)) from exc
        if cancellation:
            cancellation.raise_if_cancelled()
        return cls(path.resolve(), stat.st_size, stat.st_mtime_ns, digest.hexdigest())

    def is_current(self) -> bool:
        try:
            return self == SourceFileIdentity.capture(self.path)
        except MetadataIOError:
            return False


class CancellationToken:
    """Thread-safe cooperative token with no Qt dependency."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.CANCELLED))


@dataclass(frozen=True)
class AtomicWriteResult:
    destination: Path
    bytes_written: int


_LINK_FALLBACK_ERRNOS = frozenset({
    errno.EPERM, errno.EACCES, errno.EXDEV, errno.ENOSYS, errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
})


def _publish_no_replace_windows(temporary: Path, destination: Path) -> None:
    """Move a complete sibling temporary without replacing an existing path."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    move_file.restype = ctypes.c_int
    if move_file(str(temporary), str(destination), 0):
        return
    code = ctypes.get_last_error()
    if code in {80, 183}:  # ERROR_FILE_EXISTS / ERROR_ALREADY_EXISTS
        raise FileExistsError(str(destination))
    if code in {5, 32, 33}:
        raise PermissionError(code, "no-replace publication denied", str(destination))
    raise OSError(code, "no-replace publication failed", str(destination))


def _publish_no_replace_macos(temporary: Path, destination: Path) -> None:
    """Use macOS renamex_np(RENAME_EXCL) when hard links are unavailable.

    renamex_np(from, to, RENAME_EXCL) is macOS's exact equivalent of Linux's
    renameat2(RENAME_NOREPLACE): an atomic rename that fails with EEXIST rather
    than replacing an existing destination. APFS/HFS+ support hard links, so in
    practice ``os.link`` succeeds and this path is unreached; it exists so a
    filesystem that genuinely rejects links still gets a no-replace publish
    rather than an ``unsupported_publication`` error.
    """
    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(library, "renamex_np", None)
    if renamex_np is None:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.UNSUPPORTED_PUBLICATION))
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    RENAME_EXCL = 0x00000004
    result = renamex_np(os.fsencode(temporary), os.fsencode(destination), RENAME_EXCL)
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(str(destination))
    if code in {errno.EACCES, errno.EPERM}:
        raise PermissionError(code, os.strerror(code), str(destination))
    if code in {errno.ENOSYS, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL)}:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.UNSUPPORTED_PUBLICATION))
    raise OSError(code, os.strerror(code), str(destination))


def _publish_no_replace_posix(temporary: Path, destination: Path) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE) when hard links are unavailable."""
    if sys.platform == "darwin":
        _publish_no_replace_macos(temporary, destination)
        return

    import ctypes

    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.UNSUPPORTED_PUBLICATION))
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1)
    if result == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(str(destination))
    if code in {errno.EACCES, errno.EPERM}:
        raise PermissionError(code, os.strerror(code), str(destination))
    if code in {errno.ENOSYS, errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL),
                getattr(errno, "EOPNOTSUPP", errno.EINVAL)}:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.UNSUPPORTED_PUBLICATION))
    raise OSError(code, os.strerror(code), str(destination))


def _publish_no_replace(temporary: Path, destination: Path) -> None:
    """Atomically publish a validated file while preserving concurrent creates.

    A hard link is the cheapest create-if-absent primitive. Filesystems that do
    not support links use a native no-replace rename; neither path exposes a
    partially copied destination.
    """
    try:
        os.link(temporary, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            raise FileExistsError(str(destination)) from exc
        if exc.errno not in _LINK_FALLBACK_ERRNOS:
            raise
        if os.name == "nt":
            _publish_no_replace_windows(temporary, destination)
        else:
            _publish_no_replace_posix(temporary, destination)
    else:
        temporary.unlink()


def atomic_write_bytes(
    destination: Path,
    data: bytes,
    *,
    overwrite: bool = False,
    validator: Callable[[Path], bool | None] | None = None,
    cancellation: CancellationToken | None = None,
) -> AtomicWriteResult:
    """Publish exact bytes through a validated sibling temporary file.

    Overwrite consent is explicit at this boundary.  The final path is never
    created or replaced when validation, cancellation, flush, fsync, or write
    fails.
    """
    destination = Path(destination)
    parent = destination.parent
    if not destination.name or not parent.exists() or not parent.is_dir():
        raise MetadataIOError(IOErrorInfo(IOErrorKind.DESTINATION_INVALID))
    if destination.exists() and not overwrite:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.DESTINATION_EXISTS))
    if cancellation:
        cancellation.raise_if_cancelled()
    fd = -1
    temporary: Path | None = None
    try:
        fd, raw = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(parent))
        temporary = Path(raw)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
        if cancellation:
            cancellation.raise_if_cancelled()
        if temporary.read_bytes() != data:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.READBACK_FAILED))
        if validator is not None and validator(temporary) is False:
            raise MetadataIOError(IOErrorInfo(IOErrorKind.READBACK_FAILED))
        if cancellation:
            cancellation.raise_if_cancelled()
        if overwrite:
            os.replace(temporary, destination)
        else:
            _publish_no_replace(temporary, destination)
        temporary = None
        return AtomicWriteResult(destination, len(data))
    except MetadataIOError:
        raise
    except PermissionError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.PERMISSION_DENIED)) from exc
    except FileExistsError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.DESTINATION_EXISTS)) from exc
    except OSError as exc:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.WRITE_FAILED)) from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def resolve_workspace_scope(workspace, scope: IOScope,
                            *, ordered_items: Iterable[object] | None = None) -> tuple[int, ...]:
    """Resolve one explicit scope to deduplicated IDs without changing Apply."""
    if scope is IOScope.SELECTED:
        items = workspace.selected_tracks()
    elif scope is IOScope.VISIBLE:
        items = list(ordered_items) if ordered_items is not None else workspace.visible_tracks()
        visible = {workspace.item_id(item) for item in workspace.visible_tracks()}
        items = [item for item in items if workspace.item_id(item) in visible]
    elif scope is IOScope.CHANGED:
        items = workspace.changed_tracks()
    elif scope is IOScope.ALL_LOADED:
        items = list(ordered_items) if ordered_items is not None else workspace.tracks
    else:
        raise MetadataIOError(IOErrorInfo(IOErrorKind.INVALID_FORMAT))
    return tuple(dict.fromkeys(workspace.item_id(item) for item in items))
