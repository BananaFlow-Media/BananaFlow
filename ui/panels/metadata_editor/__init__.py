"""
Tag Editor panel package.

Split from the old monolithic ui/panels/metadata_editor_panel.py:
  panel.py         — MetadataEditorPanel (toolbar, splitter, wiring)
  explorer_view.py — Win11-Explorer-style table widgets
  dialogs.py       — column picker / auto-arrange / clean settings
  tree.py          — folder tree with physical-move drag & drop
  widgets.py       — OpRow and other small building blocks
  shared.py        — constants, QSS builders, check-mark painter
"""

from .panel import MetadataEditorPanel

__all__ = ["MetadataEditorPanel"]
