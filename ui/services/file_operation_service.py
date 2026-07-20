"""Safe, root-bounded filesystem operations for the Tag Editor Explorer."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.metadata_processor import send_to_recycle_bin


class FileOperationError(Exception):
    """A user-actionable filesystem operation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileProperties:
    path: Path
    is_directory: bool
    size_bytes: int
    modified_at: datetime
    created_at: datetime
    is_symlink: bool


class FileOperationService:
    """Perform Explorer operations without allowing an operation to escape root.

    The service deliberately does not read a file's contents. In particular it
    avoids opening cloud placeholders, and rejects a placeholder before an
    operation that could cause a provider to hydrate it.
    """

    _INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    _RESERVED_NAMES = frozenset({
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    })
    _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
    _FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000

    def __init__(self, root: Path | None = None) -> None:
        self._root: Path | None = None
        if root is not None:
            self.set_root(root)

    @property
    def root(self) -> Path | None:
        return self._root

    def set_root(self, root: Path) -> None:
        root = Path(root)
        if not root.is_dir():
            raise FileOperationError("invalid_root", f"Workspace root is not a folder: {root}")
        self._root = root.resolve()

    def open_file(self, path: Path) -> None:
        path = self._existing_workspace_path(path)
        self._reject_cloud_placeholder(path)
        if path.is_dir():
            raise FileOperationError("not_a_file", f"Cannot open a folder as a file: {path}")
        if os.name != "nt" or not hasattr(os, "startfile"):
            raise FileOperationError("unsupported_platform", "Opening files is supported on Windows only")
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows shell association

    def reveal_in_explorer(self, path: Path) -> None:
        path = self._existing_workspace_path(path)
        if os.name != "nt":
            raise FileOperationError("unsupported_platform", "Reveal in Explorer is supported on Windows only")
        subprocess.Popen(["explorer.exe", "/select,", str(path)])

    def copy_path(self, path: Path) -> str:
        return str(self._existing_workspace_path(path))

    def rename(self, path: Path, new_name: str) -> Path:
        path = self._existing_workspace_path(path, allow_root=False)
        self._reject_cloud_placeholder(path)
        name = self._validate_name(new_name)
        destination = path.with_name(name)
        self._validate_destination(destination)
        if destination == path:
            return path
        if destination.exists() and destination != path:
            raise FileOperationError("destination_exists", f"A file already exists at: {destination}")
        try:
            path.rename(destination)
        except OSError as exc:
            raise FileOperationError("rename_failed", str(exc)) from exc
        return destination

    def move(self, source: Path, destination: Path) -> Path:
        source = self._existing_workspace_path(source, allow_root=False)
        self._reject_cloud_placeholder(source)
        destination = Path(destination)
        self._validate_destination(destination)
        if destination.exists():
            raise FileOperationError("destination_exists", f"A file already exists at: {destination}")
        if source.is_dir() and self._is_relative_to(destination, source):
            raise FileOperationError("recursive_move", "Cannot move a folder into itself")
        try:
            shutil.move(str(source), str(destination))
        except OSError as exc:
            raise FileOperationError("move_failed", str(exc)) from exc
        return destination

    def create_folder(self, parent: Path, name: str) -> Path:
        parent = self._existing_workspace_path(parent)
        if not parent.is_dir():
            raise FileOperationError("not_a_folder", f"Not a folder: {parent}")
        destination = parent / self._validate_name(name)
        self._validate_destination(destination)
        if destination.exists():
            raise FileOperationError("destination_exists", f"A folder already exists at: {destination}")
        try:
            destination.mkdir()
        except OSError as exc:
            raise FileOperationError("create_folder_failed", str(exc)) from exc
        return destination

    def recycle(self, path: Path) -> None:
        path = self._existing_workspace_path(path, allow_root=False)
        self._reject_cloud_placeholder(path)
        try:
            send_to_recycle_bin(path)
        except OSError as exc:
            raise FileOperationError("recycle_failed", str(exc)) from exc

    def properties(self, path: Path) -> FileProperties:
        path = self._existing_workspace_path(path)
        self._reject_cloud_placeholder(path)
        try:
            info = path.lstat()
        except OSError as exc:
            raise FileOperationError("properties_failed", str(exc)) from exc
        return FileProperties(
            path=path,
            is_directory=stat.S_ISDIR(info.st_mode),
            size_bytes=info.st_size,
            modified_at=datetime.fromtimestamp(info.st_mtime),
            created_at=datetime.fromtimestamp(info.st_ctime),
            is_symlink=path.is_symlink(),
        )

    def _existing_workspace_path(self, path: Path, *, allow_root: bool = True) -> Path:
        path = Path(path)
        if not path.exists():
            raise FileOperationError("missing", f"Path no longer exists: {path}")
        absolute = path.absolute()
        resolved = path.resolve()
        if self._root is not None and not self._is_relative_to(resolved, self._root):
            raise FileOperationError("root_escape", "The selected path is outside this workspace")
        if not allow_root and self._root is not None and resolved == self._root:
            raise FileOperationError("root_operation", "The workspace root cannot be modified")
        # Containment is checked against the resolved target, but mutations use
        # the selected directory entry so renaming a safe in-root symlink
        # renames the link rather than silently changing its target.
        return absolute

    def _validate_destination(self, destination: Path) -> None:
        destination = Path(destination)
        parent = destination.parent
        if not parent.exists() or not parent.is_dir():
            raise FileOperationError("missing_parent", f"Destination folder does not exist: {parent}")
        resolved_parent = parent.resolve()
        if self._root is not None and not self._is_relative_to(resolved_parent, self._root):
            raise FileOperationError("root_escape", "The destination is outside this workspace")

    def _validate_name(self, name: str) -> str:
        name = str(name).strip()
        stem = name.rstrip(". ").split(".", 1)[0].upper()
        if (
            not name or name in {".", ".."} or name != name.rstrip(". ")
            or self._INVALID_NAME.search(name) or stem in self._RESERVED_NAMES
        ):
            raise FileOperationError("invalid_name", "The name is not valid on Windows")
        return name

    def _reject_cloud_placeholder(self, path: Path) -> None:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        if attributes & (self._FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS | self._FILE_ATTRIBUTE_RECALL_ON_OPEN):
            raise FileOperationError("cloud_placeholder", "This cloud file must be available locally first")

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
