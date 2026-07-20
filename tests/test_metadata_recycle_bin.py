from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest

import core.metadata_processor as metadata_processor

# These tests cover the Windows shell recycle-bin fallback, so they fake
# os.name = "nt". That fake is only safe on a real Windows host: os is a single
# process-wide module object, so the fake also makes pathlib.Path() build a
# WindowsPath — and WindowsPath cannot be instantiated on POSIX, so
# Path("C:/...") below raises NotImplementedError. Worse, in a single-process
# run the failure's own longrepr calls Path() while the fake is still active,
# turning it into a session-ending INTERNALERROR that takes the whole suite
# down (observed on ubuntu CI). tests/test_bundled_ffmpeg_discovery.py documents
# this exact hazard. Skip on non-Windows: there is nothing platform-neutral to
# assert here, and the real Windows path is covered on the Windows CI legs.
pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows shell recycle-bin behaviour; faking os.name on POSIX breaks pathlib",
)


def _block_send2trash_import(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "send2trash":
            raise ImportError("send2trash unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_windows_recycle_bin_uses_shell_when_send2trash_missing(monkeypatch):
    _block_send2trash_import(monkeypatch)
    monkeypatch.setattr(metadata_processor.os, "name", "nt", raising=False)
    called = []
    monkeypatch.setattr(
        metadata_processor,
        "_send_to_windows_recycle_bin",
        lambda path: called.append(path),
    )

    metadata_processor.send_to_recycle_bin(Path("C:/music/song.mp3"))

    assert called == [Path("C:/music/song.mp3")]


def test_windows_recycle_bin_failure_does_not_fall_back_to_unlink(monkeypatch):
    _block_send2trash_import(monkeypatch)
    monkeypatch.setattr(metadata_processor.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        metadata_processor,
        "_send_to_windows_recycle_bin",
        lambda path: (_ for _ in ()).throw(OSError("shell failed")),
    )

    with pytest.raises(OSError, match="shell failed"):
        metadata_processor.send_to_recycle_bin(Path("C:/music/song.mp3"))
