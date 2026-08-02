from ui.controllers.metadata_controller import MetadataController


def test_auto_arrange_sequence_does_not_repeat_core_title_or_track_actions():
    steps = MetadataController._auto_sequence_steps([
        "title_strip", "track_num", "normalize_spaces", "title_full",
    ])

    assert [step.action_id for step in steps] == [
        "tag.auto_arrange.v1",
        "tag.normalize_spaces.v1",
        "tag.title_from_filename.v1",
    ]
    assert steps[-1].parameters == {"strip_numbering": False}
