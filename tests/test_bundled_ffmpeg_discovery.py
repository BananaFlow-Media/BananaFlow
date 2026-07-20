"""
tests/test_bundled_ffmpeg_discovery.py  –  get_bundled_ffmpeg_dir() layouts
=============================================================================
Phase 5 release-build smoke testing found a real bug via a genuine
PyInstaller build (scripts/build_windows.ps1), not from source: PyInstaller
6.x's default one-folder layout collects bundled binaries into an
executable-adjacent "_internal" folder rather than dropping them next to
the EXE itself. bananaflow.spec's `binaries=[(ffmpeg_path, '.')]` entry landed
ffmpeg.exe/ffprobe.exe in dist/bananaflow/_internal/, but
get_bundled_ffmpeg_dir() only ever checked next to the EXE (or a couple of
alternative source-checkout layouts) — a real release build would have
silently shipped with FFmpeg present on disk but never found, degrading
straight to the "FFmpeg not found" preflight warning despite the app
supposedly bundling it out of the box.

core.runtime_components already handles this exact PyInstaller behavior
for the PO Token Provider/Deno discovery via `sys._MEIPASS` (the
PyInstaller-guaranteed pointer to wherever the real internal contents
directory is, regardless of its name) — this pins the same fix applied to
get_bundled_ffmpeg_dir().

These tests run with whichever binary-name convention the *real* host OS
uses (ffmpeg.exe on Windows, ffmpeg with no suffix elsewhere) rather than
forcing os.name. Faking os.name to "nt" on a real POSIX host is not a
safe, contained monkeypatch: os is a single process-wide module object
(not a per-caller copy), so `os.name == "nt"` becomes true for pytest's
*own* internals too — its cache/tmpdir machinery calls pathlib.Path(),
which reads the real os.name to pick WindowsPath vs PosixPath, and
WindowsPath construction on an actual Linux runner raises
NotImplementedError. That crashed this exact suite on ubuntu-latest CI
the first time this file existed (a session-ending INTERNALERROR, not a
normal test failure) — the same class of mistake as the platform-blind
Windows-only chrome-profile test fixed alongside this one.
"""

from __future__ import annotations

import os
import sys

import pytest

from utils.paths import get_bundled_ffmpeg_dir

_SUFFIX = ".exe" if os.name == "nt" else ""


def _make_ffmpeg_pair(directory) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"ffmpeg{_SUFFIX}").write_bytes(b"")
    (directory / f"ffprobe{_SUFFIX}").write_bytes(b"")


def _make_frozen(monkeypatch, install_dir, meipass=None):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        sys, "executable", str(install_dir / f"bananaflow{_SUFFIX}"), raising=False
    )
    if meipass is not None:
        monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    else:
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)


class TestBundledFfmpegDiscovery:

    def test_source_checkout_finds_packaging_ffmpeg(self, tmp_path, monkeypatch):
        """Non-frozen (running from source): the dev-layout fallback."""
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(
            "utils.paths.get_install_dir", lambda: tmp_path
        )
        _make_ffmpeg_pair(tmp_path / "packaging" / "ffmpeg")

        result = get_bundled_ffmpeg_dir()
        assert result == tmp_path / "packaging" / "ffmpeg"

    def test_frozen_next_to_exe_is_found(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "dist" / "bananaflow"
        _make_frozen(monkeypatch, install_dir)
        _make_ffmpeg_pair(install_dir)

        result = get_bundled_ffmpeg_dir()
        assert result == install_dir

    def test_frozen_pyinstaller_internal_layout_is_found(self, tmp_path, monkeypatch):
        """The real bug this file pins: PyInstaller 6.x's one-folder
        build drops bundled binaries into an executable-adjacent
        "_internal" directory, discoverable via sys._MEIPASS."""
        install_dir = tmp_path / "dist" / "bananaflow"
        internal_dir = install_dir / "_internal"
        _make_frozen(monkeypatch, install_dir, meipass=internal_dir)
        _make_ffmpeg_pair(internal_dir)

        result = get_bundled_ffmpeg_dir()
        assert result == internal_dir

    def test_frozen_with_no_ffmpeg_anywhere_returns_none(self, tmp_path, monkeypatch):
        install_dir = tmp_path / "dist" / "bananaflow"
        internal_dir = install_dir / "_internal"
        install_dir.mkdir(parents=True)
        internal_dir.mkdir(parents=True)
        _make_frozen(monkeypatch, install_dir, meipass=internal_dir)

        assert get_bundled_ffmpeg_dir() is None

    def test_frozen_requires_both_binaries_present(self, tmp_path, monkeypatch):
        """A folder with only one of the pair must not count as bundled."""
        install_dir = tmp_path / "dist" / "bananaflow"
        install_dir.mkdir(parents=True)
        _make_frozen(monkeypatch, install_dir)
        (install_dir / f"ffmpeg{_SUFFIX}").write_bytes(b"")
        # ffprobe deliberately missing.

        assert get_bundled_ffmpeg_dir() is None

    def test_internal_layout_takes_priority_only_when_exe_adjacent_is_empty(
        self, tmp_path, monkeypatch
    ):
        """Real layout, both candidates populated (paranoia check): the
        executable-adjacent copy is preferred since it's checked first."""
        install_dir = tmp_path / "dist" / "bananaflow"
        internal_dir = install_dir / "_internal"
        _make_frozen(monkeypatch, install_dir, meipass=internal_dir)
        _make_ffmpeg_pair(install_dir)
        _make_ffmpeg_pair(internal_dir)

        result = get_bundled_ffmpeg_dir()
        assert result == install_dir
