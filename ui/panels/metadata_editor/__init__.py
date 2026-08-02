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


# The consolidated All Actions page keeps quick actions grouped by purpose.
# Album-artist copying and sequential numbering organize tags; they do not
# clean text. Normalize the legacy quick-action map once at the package boundary
# so every import path receives the same canonical grouping.
def _canonical_common_action_sections(sections):
    organized = ("album_artist", "number_tracks")
    result = []
    for heading, operations in sections:
        if heading == "meta_section_text_cleanup":
            operations = tuple(op for op in operations if op not in organized)
            result.append((heading, operations))
            result.append(("meta_action_category_organize", organized))
        else:
            result.append((heading, operations))
    return tuple(result)


MetadataEditorPanel._COMMON_ACTION_SECTIONS = _canonical_common_action_sections(
    MetadataEditorPanel._COMMON_ACTION_SECTIONS
)

__all__ = ["MetadataEditorPanel"]
