from core.output_layout import decide_output_layout, english_category_name
from core.playlist_parser import UrlKind


def _folder(*, release_type: str, singles_subfolder: bool, **overrides) -> str:
    values = dict(
        source_kind=UrlKind.ARTIST,
        release_type=release_type,
        collection_title="",
        album="",
        parent_artist="Idan Raichel",
        artist="Idan Raichel",
        singles_subfolder=singles_subfolder,
    )
    values.update(overrides)
    return decide_output_layout(**values).render_folder(
        localize_category=english_category_name,
    )


def test_singles_subfolder_enabled():
    assert _folder(
        release_type="single", singles_subfolder=True,
    ) == "Idan Raichel/Singles & EPs"


def test_singles_subfolder_disabled():
    assert _folder(
        release_type="single", singles_subfolder=False,
    ) == "Idan Raichel"


def test_album_structure_is_unaffected_by_singles_setting():
    assert _folder(
        release_type="album", singles_subfolder=False,
        album="Project Album", collection_title="Project Album",
    ) == "Idan Raichel/Albums/Project Album"


def test_collection_subfolders_disabled_yields_empty_string():
    assert _folder(
        release_type="album", singles_subfolder=True,
        album="Project Album", collection_title="Project Album",
        playlist_subfolders=False,
    ) == ""
