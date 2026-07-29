"""
ui/panels/metadata_editor/prompts.py  –  the Tag Editor's modal surface
==============================================================================
One place for every blocking prompt the Tag Editor can raise.

Modules must call these through the module (``prompts.confirm(...)``) rather
than importing the names directly.  ``from ... import confirm`` binds at import
time, so a test that patches the helper only intercepts the *one* module it
patched — and the Tag Editor raises the same four prompts from four different
files.  Going through the module means the lookup happens at call time, so a
single patch of ``ui.panels.metadata_editor.prompts`` covers all of them and
keeps working when a handler moves between files.

That matters here specifically: an unpatched prompt does not fail a test, it
*hangs* it on a modal dialog that no one can close under the offscreen
platform.
"""

from __future__ import annotations

from ui.dialogs.styled_dialog import (  # noqa: F401  (re-exported by design)
    confirm,
    get_text,
    show_info,
    show_warning,
)

__all__ = ["confirm", "get_text", "show_info", "show_warning"]
