from ui.panels.metadata_editor import MetadataEditorPanel


def test_common_actions_separate_cleanup_from_tag_organization():
    sections = dict(MetadataEditorPanel._COMMON_ACTION_SECTIONS)

    assert sections["meta_section_text_cleanup"] == (
        "normalize_spaces",
        "strip_junk",
        "replace_text",
        "change_case",
    )
    assert sections["meta_action_category_organize"] == (
        "album_artist",
        "number_tracks",
    )
