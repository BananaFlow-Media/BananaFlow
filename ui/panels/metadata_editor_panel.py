"""
ui/panels/metadata_editor_panel.py  –  compatibility re-export shim
====================================================================
The Tag Editor panel was split into ui/panels/metadata_editor/ (panel.py,
explorer_view.py, dialogs.py, tree.py, widgets.py, shared.py) — a single
4,500-line module mixing five widget classes, three dialogs, and the panel
itself was hard to navigate and maintain.

This module only re-exports the public entry point (MetadataEditorPanel) and
the two style helpers an existing test imports directly, so nothing outside
ui/panels/metadata_editor/ needs to change. New code should import from
ui.panels.metadata_editor directly instead of this shim.
"""

from __future__ import annotations

from ui.panels.metadata_editor.panel import MetadataEditorPanel
from ui.panels.metadata_editor.shared import btn_style as _btn_style
from ui.panels.metadata_editor.shared import primary_btn_style as _primary_btn_style

__all__ = ["MetadataEditorPanel", "_btn_style", "_primary_btn_style"]
