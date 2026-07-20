"""Suite-wide safety net: no test may write into the real user profile.

The Phase 15 audit caught a live example of why this is needed. A crashed test
run left a genuine draft file in the developer's own ``~/.bananaflow/tag_drafts``,
pointing at a long-deleted pytest temp directory — and that stray file went on
to hang the *packaged release gate* on a modal recovery prompt. The test suite
had quietly been treating the developer's home directory as scratch space.

Every app-data root is therefore redirected into a per-test temporary directory.
This is defence in depth, not a substitute for isolation inside a test: tests
that care about these paths (see ``test_draft_location_migration.py``) still set
them explicitly. What this guarantees is that a test which *forgets* leaks into
a temp directory instead of into real user data.

``tmp_path_factory`` is used rather than ``tmp_path`` on purpose: sharing the
test's own ``tmp_path`` would drop ``.bananaflow`` directories into the very folder
many tests enumerate and assert over.
"""

from __future__ import annotations

import platform

import pytest


# --------------------------------------------------------------------------- #
# Work around a CPython 3.11.9 bug that crashes qfluentwidgets at import time.
#
# qfluentwidgets imports `darkdetect`, which on Windows calls
# platform.win32_ver() at import. On Python 3.11.9 in the GitHub windows-latest
# sandbox, platform._syscmd_ver()'s `cmd /c ver` subprocess returns nothing, so
# the function's own `info.strip()` raises AttributeError on None -- taking down
# collection for every test file that imports a UI widget (observed: 15 failures
# in test_converter alone on the 3.10/3.11 legs). It is a stdlib/environment
# bug, not ours, and it does not reproduce on the Python 3.12 the release is
# built with. Guarded here, at conftest import (before any test imports
# qfluentwidgets), so the test gate reflects the product rather than a CI
# interpreter quirk. Test-only; production is untouched.
# --------------------------------------------------------------------------- #
_orig_syscmd_ver = getattr(platform, "_syscmd_ver", None)
if _orig_syscmd_ver is not None:
    def _safe_syscmd_ver(*args, **kwargs):  # pragma: no cover - env-specific
        try:
            return _orig_syscmd_ver(*args, **kwargs)
        except AttributeError:
            # Same shape win32_ver() expects: (system, release, version).
            return "", "", ""

    platform._syscmd_ver = _safe_syscmd_ver


@pytest.fixture(autouse=True)
def _isolate_user_data_dirs(tmp_path_factory, monkeypatch):
    """Redirect APPDATA / HOME / USERPROFILE / XDG_CONFIG_HOME into a temp dir."""
    base = tmp_path_factory.mktemp("userprofile")
    home = base / "home"
    appdata = base / "AppData" / "Roaming"
    home.mkdir(parents=True, exist_ok=True)
    appdata.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(base / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(appdata))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    # ntpath.expanduser prefers USERPROFILE, but falls back to this pair; leaving
    # them set would let Path.home() find the real profile anyway.
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    yield base
