"""Process-local runtime mode flags.

Currently this exists for exactly one thing: letting the hidden packaged smoke
(``bananaflow.exe --internal-smoke-test tag-editor``) tell the production startup
path that **nobody is there to answer a modal dialog**.

Why this is not an environment variable
--------------------------------------
The flag is set by ``core.internal_smoke_test`` in its own process and read in
the same process. Keeping it as module state rather than an env var means a
value inherited from the user's environment — or from a parent process — can
never switch a real user's recovery prompt off. There is no way to reach this
from outside the process that deliberately set it.

This is *not* a general "non-interactive" switch, and must not grow into one.
Production startup still shows every recovery prompt; the smoke suppresses only
the specific blocking prompt it cannot answer, and reports what it found instead
(see ``ui/app_window.py:_check_tag_apply_recovery``).
"""

from __future__ import annotations

_internal_smoke = False


def set_internal_smoke(enabled: bool) -> None:
    """Mark this process as the internal packaged smoke. Never call from production."""
    global _internal_smoke
    _internal_smoke = bool(enabled)


def is_internal_smoke() -> bool:
    """True only inside a process that explicitly declared itself the smoke."""
    return _internal_smoke
