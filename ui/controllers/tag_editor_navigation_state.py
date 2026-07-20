"""Root-bounded navigation history for the Tag Editor's Phase 3 browser."""

from __future__ import annotations

import os
from pathlib import Path


class TagEditorNavigationState:
    """Maintain current folder and Back/Forward history without filesystem I/O."""

    def __init__(self) -> None:
        self._root: Path | None = None
        self._current: Path | None = None
        self._back: list[Path] = []
        self._forward: list[Path] = []

    @property
    def root(self) -> Path | None:
        return self._root

    @property
    def current(self) -> Path | None:
        return self._current

    @property
    def can_go_back(self) -> bool:
        return bool(self._back)

    @property
    def can_go_forward(self) -> bool:
        return bool(self._forward)

    @property
    def can_go_up(self) -> bool:
        return self._current is not None and self._current != self._root

    def set_root(self, root: Path | None) -> None:
        self._root = self._normalise(root) if root is not None else None
        self._current = self._root
        self._back.clear()
        self._forward.clear()

    def navigate(self, folder: Path) -> bool:
        """Move to a root-contained folder, recording one history entry."""
        folder = self._normalise(folder)
        if not self._contains(folder) or folder == self._current:
            return False
        if self._current is not None:
            self._back.append(self._current)
        self._current = folder
        self._forward.clear()
        return True

    def back(self) -> bool:
        if not self._back:
            return False
        if self._current is not None:
            self._forward.append(self._current)
        self._current = self._back.pop()
        return True

    def forward(self) -> bool:
        if not self._forward:
            return False
        if self._current is not None:
            self._back.append(self._current)
        self._current = self._forward.pop()
        return True

    def up(self) -> bool:
        if not self.can_go_up or self._current is None:
            return False
        return self.navigate(self._current.parent)

    def remap_folder(self, source: Path, destination: Path) -> None:
        """Rebase current and history entries after a physical folder rename/move.

        The Explorer owns the filesystem operation.  Navigation only remaps the
        already-known paths and then discards entries that no longer name valid
        in-root folders.
        """
        source = self._normalise(source)
        destination = self._normalise(destination)
        if self._root is None or not self._contains(destination):
            self.reconcile_filesystem()
            return

        self._current = self._remap_path(self._current, source, destination)
        self._back = [self._remap_path(path, source, destination) for path in self._back]
        self._forward = [self._remap_path(path, source, destination) for path in self._forward]
        self.reconcile_filesystem()

    def reconcile_after_delete(self, deleted: Path) -> None:
        """Recover a valid current folder and clean history after a deletion."""
        deleted = self._normalise(deleted)
        preferred = self._current
        if preferred is not None and self._is_within(preferred, deleted):
            preferred = deleted.parent
        self.reconcile_filesystem(preferred_current=preferred)

    def reconcile_filesystem(self, *, preferred_current: Path | None = None) -> None:
        """Drop stale navigation entries and keep the current folder usable.

        This is intentionally called only after Explorer filesystem operations;
        ordinary navigation remains filesystem-I/O-free.
        """
        if self._root is None:
            self._current = None
            self._back.clear()
            self._forward.clear()
            return

        preferred = self._normalise(preferred_current) if preferred_current is not None else self._current
        self._current = self._nearest_valid_in_root(preferred)
        if self._current is None:
            self._back.clear()
            self._forward.clear()
            return

        seen = {self._current}
        self._back = self._clean_history(self._back, seen)
        self._forward = self._clean_history(self._forward, seen)

    def _contains(self, folder: Path) -> bool:
        if self._root is None:
            return False
        try:
            common = os.path.commonpath((
                os.path.normcase(os.fspath(self._root)),
                os.path.normcase(os.fspath(folder)),
            ))
        except ValueError:
            return False
        return common == os.path.normcase(os.fspath(self._root))

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @classmethod
    def _remap_path(cls, path: Path | None, source: Path, destination: Path) -> Path | None:
        if path is None or not cls._is_within(path, source):
            return path
        return destination / path.relative_to(source)

    def _nearest_valid_in_root(self, folder: Path | None) -> Path | None:
        if self._root is None:
            return None
        candidate = self._normalise(folder) if folder is not None else self._root
        while self._contains(candidate):
            if candidate.is_dir():
                return candidate
            if candidate == self._root:
                break
            candidate = candidate.parent
        return self._root if self._root.is_dir() else None

    def _clean_history(self, entries: list[Path], seen: set[Path]) -> list[Path]:
        """Keep only valid, unique entries, preferring the nearest history item."""
        kept: list[Path] = []
        for path in reversed(entries):
            path = self._normalise(path)
            if path in seen or not self._contains(path) or not path.is_dir():
                continue
            seen.add(path)
            kept.append(path)
        kept.reverse()
        return kept

    @staticmethod
    def _normalise(folder: Path) -> Path:
        """Collapse ``.``/``..`` and normalize Windows casing without requiring existence."""
        return Path(os.path.normpath(os.path.abspath(os.fspath(folder))))
