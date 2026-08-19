"""Quality-order regression for YouTube thumbnail fallback candidates."""

from utils.artwork_cleaner import get_youtube_thumbnail_candidates


def test_standard_hq_input_still_tries_maxres_first():
    video_id = "6SYvCsbal2o"
    candidates = get_youtube_thumbnail_candidates(
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    )

    assert candidates[0] == f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    assert candidates[1] == f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg"
    assert candidates[2] == f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


def test_nonstandard_ytimg_variant_is_preserved_first():
    video_id = "6SYvCsbal2o"
    special = f"https://i.ytimg.com/vi/{video_id}/hqdefault_live.jpg?custom=1"
    candidates = get_youtube_thumbnail_candidates(special)

    assert candidates[0] == special
    assert candidates[1] == f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
