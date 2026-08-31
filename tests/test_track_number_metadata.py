"""
tests/test_track_number_metadata.py  –  Album track/disc position (issue #65)
=============================================================================
Album downloads carried the collection position into the *filename* but not
into the file's tags, so players and libraries that sort by the embedded
track number saw an unordered album.

Two independent things had to hold, and these tests pin both:

1. The yt-dlp ``postprocessor_args`` we hand over are actually delivered.
   They were keyed by post-processor *class* name ("FFmpegMetadata"), but
   yt-dlp resolves them through ``PostProcessor.pp_key()``, which strips the
   ``FFmpeg`` prefix ("metadata"). The lookup missed silently — no warning,
   no error, every forced ``-metadata`` argument dropped. So these tests run
   the resolution through yt-dlp's own code rather than asserting on our
   literal dict keys, which would happily pass while the real lookup misses.

2. The position ends up in the finished file's tags whatever route the
   download took. That is checked by reading the tags back with mutagen, not
   by inspecting request objects or filenames — the request was always
   correct, which is exactly why the bug survived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.audio_fixtures import make_empty_audio


# ──────────────────────────────────────────────────────────────────────────────
# 1. yt-dlp actually receives the arguments
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_through_ytdlp(postprocessor_args: dict, pp_cls) -> list[str]:
    """Ask yt-dlp itself what `pp_cls` would receive from `postprocessor_args`.

    Mirrors FFmpegPostProcessor.real_run_ffmpeg, which asks for the first
    output file's args with keys ['_o1', '_o', ''].
    """
    from yt_dlp.utils import _configuration_args

    return _configuration_args(
        pp_cls.pp_key(), postprocessor_args, "ffmpeg", ["_o1", "_o", ""],
    )


def _album_request(**overrides):
    from core.downloader import DownloadRequest

    kwargs = dict(
        url="https://example.com/track",
        output_dir=".",
        forced_title="Track Title",
        forced_artist="Some Artist",
        forced_album="Some Album",
        forced_index=3,
        filename_index=3,
        forced_total=12,
    )
    kwargs.update(overrides)
    return DownloadRequest(**kwargs)


def test_audio_postprocessor_args_reach_ytdlp():
    """The regression guard: yt-dlp's own lookup must find our track number."""
    from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP, FFmpegMetadataPP

    from core.downloader import DownloadEngine

    pp_args = DownloadEngine._audio_opts(_album_request())["postprocessor_args"]

    for pp_cls in (FFmpegMetadataPP, FFmpegExtractAudioPP):
        delivered = _resolve_through_ytdlp(pp_args, pp_cls)
        assert delivered, f"{pp_cls.__name__} receives no postprocessor args at all"
        assert "track=3/12" in delivered, f"{pp_cls.__name__} misses the track number"


def test_video_postprocessor_args_reach_ytdlp():
    from yt_dlp.postprocessor.ffmpeg import FFmpegMetadataPP, FFmpegVideoConvertorPP

    from core.downloader import DownloadEngine, MediaType

    req = _album_request(media_type=MediaType.VIDEO)
    pp_args = DownloadEngine._video_opts(req)["postprocessor_args"]

    for pp_cls in (FFmpegMetadataPP, FFmpegVideoConvertorPP):
        delivered = _resolve_through_ytdlp(pp_args, pp_cls)
        assert delivered, f"{pp_cls.__name__} receives no postprocessor args at all"
        assert "track=3/12" in delivered, f"{pp_cls.__name__} misses the track number"


def test_multi_disc_track_arg_omits_the_release_total():
    """Per-disc numbering makes the release-wide total wrong as a track total."""
    from core.downloader import _forced_metadata_args

    delivered = _forced_metadata_args(
        _album_request(forced_disc=2, forced_total=24), ("metadata",),
    )["metadata"]

    assert "track=3" in delivered
    assert "track=3/24" not in delivered
    assert "disc=2" in delivered


def test_no_argument_clears_a_field_we_have_no_value_for():
    """An empty `-metadata key=` erases what the source supplied — never emit it."""
    from core.downloader import DownloadEngine, DownloadRequest

    req = DownloadRequest(url="https://example.com/x", output_dir=".")
    pp_args = DownloadEngine._audio_opts(req)["postprocessor_args"]

    for args in pp_args.values():
        empty = [a for a in args if a.endswith("=")]
        assert not empty, f"these would clear existing metadata: {empty}"


def test_solo_download_sends_no_track_number():
    from core.downloader import DownloadEngine, DownloadRequest
    from yt_dlp.postprocessor.ffmpeg import FFmpegMetadataPP

    req = DownloadRequest(
        url="https://example.com/x", output_dir=".",
        forced_title="A Single", is_solo=True,
    )
    pp_args = DownloadEngine._audio_opts(req)["postprocessor_args"]
    delivered = _resolve_through_ytdlp(pp_args, FFmpegMetadataPP)

    assert not any(a.startswith("track=") for a in delivered)
    assert not any(a.startswith("disc=") for a in delivered)


# ──────────────────────────────────────────────────────────────────────────────
# 2. The finished file really carries the position
# ──────────────────────────────────────────────────────────────────────────────

def _stamped(tmp_path: Path, suffix: str, **overrides) -> Path:
    from core.downloader import _stamp_authoritative_position

    path = tmp_path / f"track{suffix}"
    make_empty_audio(path)
    _stamp_authoritative_position(_album_request(**overrides), str(path))
    return path


def test_mp3_gets_a_trck_frame(tmp_path):
    from mutagen.id3 import ID3

    tags = ID3(str(_stamped(tmp_path, ".mp3")))

    assert str(tags["TRCK"]) == "3/12"
    assert "TPOS" not in tags, "a single-disc release should not claim a disc"


def test_mp3_multi_disc_gets_trck_and_tpos(tmp_path):
    from mutagen.id3 import ID3

    tags = ID3(str(_stamped(tmp_path, ".mp3", forced_disc=2, forced_total=24)))

    assert str(tags["TRCK"]) == "3"
    assert str(tags["TPOS"]) == "2"


def test_flac_gets_a_tracknumber(tmp_path):
    from mutagen.flac import FLAC

    audio = FLAC(str(_stamped(tmp_path, ".flac")))

    assert audio["tracknumber"] == ["3"]
    assert audio["totaltracks"] == ["12"]


def test_m4a_gets_a_trkn_atom(tmp_path):
    from mutagen.mp4 import MP4

    audio = MP4(str(_stamped(tmp_path, ".m4a")))

    assert audio["trkn"] == [(3, 12)]


def test_container_without_a_tag_layer_is_skipped_quietly(tmp_path):
    """webm/mkv have nowhere to put a track number — not a download failure."""
    from core.downloader import _stamp_authoritative_position

    path = tmp_path / "03 - Track Title.webm"
    path.write_bytes(b"\x1a\x45\xdf\xa3")

    _stamp_authoritative_position(_album_request(), str(path))  # must not raise


def test_single_track_download_stays_unnumbered(tmp_path):
    """An independent URL must not be given an invented collection position."""
    from mutagen.id3 import ID3

    from core.downloader import DownloadRequest, _stamp_authoritative_position

    path = tmp_path / "solo.mp3"
    make_empty_audio(path)
    req = DownloadRequest(
        url="https://example.com/x", output_dir=".",
        forced_title="A Single", is_solo=True,
    )
    _stamp_authoritative_position(req, str(path))

    assert "TRCK" not in ID3(str(path))


# ──────────────────────────────────────────────────────────────────────────────
# 3. The position survives the rest of the post-download pipeline
# ──────────────────────────────────────────────────────────────────────────────

def test_position_survives_artwork_and_enrichment(tmp_path, monkeypatch):
    """The pipeline's later stages rewrite tags; the position must outlive them.

    Runs the real _run_final_pipeline with artwork embedding and MusicBrainz
    enrichment enabled, so an ordering regression (stamping before a stage
    that rewrites the tag block) fails here rather than in the field.
    """
    from mutagen.id3 import ID3

    import core.downloader as downloader_mod
    from core.downloader import DownloadEngine, MediaType

    path = tmp_path / "03 - Track Title.mp3"
    make_empty_audio(path)

    monkeypatch.setattr(downloader_mod.time, "sleep", lambda *_: None)

    # Stand in for the two stages that rewrite the tag block, without network:
    # a cover injection and an enrichment pass that adds a genre.
    def fake_embed(media_path, image_url, crop=False, pad=False):
        from mutagen.id3 import APIC, ID3NoHeaderError
        try:
            tags = ID3(media_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=b"\xff\xd8\xff"))
        tags.save(media_path, v2_version=3)
        return True

    def fake_enrich(file_path, **kwargs):
        from mutagen.id3 import TCON, ID3NoHeaderError
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TCON(encoding=3, text="Rock"))
        tags.save(file_path)
        return True

    monkeypatch.setattr("core.thumbnail_cropper.embed_custom_thumbnail", fake_embed)
    monkeypatch.setattr("core.musicbrainz_enricher.enrich_file", fake_enrich)

    req = _album_request(
        media_type=MediaType.AUDIO,
        thumbnail_url="https://example.com/cover.jpg",
        musicbrainz=True,
    )
    failures = DownloadEngine()._run_final_pipeline(req, str(path))

    assert failures == []
    tags = ID3(str(path))
    assert str(tags["TRCK"]) == "3/12"
    assert tags.getall("APIC"), "artwork must still be there too"
    assert str(tags["TCON"]) == "Rock"


def test_pipeline_reports_a_failed_stamp_without_raising(tmp_path, monkeypatch):
    """A tag-write failure is a partial-failure warning, not a lost download."""
    import core.downloader as downloader_mod
    from core.downloader import DownloadEngine

    path = tmp_path / "03 - Track Title.mp3"
    make_empty_audio(path)

    monkeypatch.setattr(downloader_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        downloader_mod, "_stamp_authoritative_position",
        lambda *_: (_ for _ in ()).throw(RuntimeError("disk on fire")),
    )

    failures = DownloadEngine()._run_final_pipeline(_album_request(), str(path))

    assert any("track number" in f for f in failures)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Position survives persistence (pause/resume, restart)
# ──────────────────────────────────────────────────────────────────────────────

def test_position_round_trips_through_the_request_codec():
    from core.download_request_codec import request_from_dict, request_to_dict

    req = _album_request(
        forced_disc=2, forced_total=24, filename_include_artist=True,
    )
    restored = request_from_dict(request_to_dict(req))

    assert restored.forced_index == 3
    assert restored.filename_index == 3
    assert restored.filename_include_artist is True
    assert restored.forced_disc == 2
    assert restored.forced_total == 24


def test_legacy_persisted_position_keeps_its_existing_filename_prefix():
    """Old paused jobs used forced_index for both tags and filenames."""
    from core.download_request_codec import request_from_dict, request_to_dict

    data = request_to_dict(_album_request())
    data.pop("filename_index")

    restored = request_from_dict(data)

    assert restored.forced_index == 3
    assert restored.filename_index == 3


def test_explicit_unnumbered_filename_survives_persistence():
    """A compilation's None is intentional, not a legacy missing field."""
    from core.download_request_codec import request_from_dict, request_to_dict

    restored = request_from_dict(request_to_dict(_album_request(filename_index=None)))

    assert restored.forced_index == 3
    assert restored.filename_index is None


# ──────────────────────────────────────────────────────────────────────────────
# 5. Filename numbering and embedded metadata agree
# ──────────────────────────────────────────────────────────────────────────────

def test_filename_template_uses_filename_index_not_track_tag(tmp_path):
    from core.downloader import DownloadEngine

    compilation = _album_request(
        output_dir=str(tmp_path), filename_index=None, forced_index=5,
        clean_filename=True,
    )
    playlist = _album_request(
        output_dir=str(tmp_path), filename_index=7, forced_index=None,
        clean_filename=True,
    )

    compilation_template = DownloadEngine()._build_ydl_opts(compilation)["outtmpl"]
    playlist_template = DownloadEngine()._build_ydl_opts(playlist)["outtmpl"]

    assert "05 - " not in Path(compilation_template).name
    assert Path(compilation_template).name == "Track Title.%(ext)s"
    assert Path(playlist_template).name == "07 - Track Title.%(ext)s"


def test_direct_and_compilation_filename_bodies_include_artist(tmp_path):
    from core.downloader import DownloadEngine

    direct = _album_request(
        output_dir=str(tmp_path), forced_index=None, filename_index=None,
        filename_include_artist=True, is_solo=True,
    )
    compilation = _album_request(
        output_dir=str(tmp_path), forced_index=5, filename_index=None,
        filename_include_artist=True, is_solo=False, clean_filename=False,
    )

    direct_template = DownloadEngine()._build_ydl_opts(direct)["outtmpl"]
    compilation_template = DownloadEngine()._build_ydl_opts(compilation)["outtmpl"]

    expected = "Some Artist - Track Title.%(ext)s"
    assert Path(direct_template).name == expected
    assert Path(compilation_template).name == expected


@pytest.mark.parametrize(
    "version",
    ["Song (Live)", "Song (Acoustic)", "Song (Radio Remix)",
     "Song - Club Edit", "Song - Original"],
)
def test_meaningful_version_labels_survive_filename_cleaning(tmp_path, version):
    from core.downloader import DownloadEngine

    request = _album_request(
        output_dir=str(tmp_path), forced_title=version, clean_filename=True,
    )

    template = DownloadEngine()._build_ydl_opts(request)["outtmpl"]

    assert Path(template).name == f"03 - {version}.%(ext)s"


def test_promotional_video_label_is_still_removed(tmp_path):
    from core.downloader import DownloadEngine
    from core.duplicate_checker import expected_stem

    request = _album_request(
        output_dir=str(tmp_path), forced_title="Song (Official Video)",
        clean_filename=True,
    )

    template = DownloadEngine()._build_ydl_opts(request)["outtmpl"]

    assert Path(template).name == "03 - Song.%(ext)s"
    assert expected_stem(
        "Song (Official Video)", "Some Artist", 3, True, False,
    ) == "03 - Song"


def test_duplicate_lookup_uses_the_downloaders_sanitized_collection_path(tmp_path):
    from core.duplicate_checker import find_duplicate

    collection = tmp_path / "Artist" / "Album - Deluxe"
    collection.mkdir(parents=True)
    expected = collection / "01 - Song.mp3"
    expected.touch()

    found = find_duplicate(
        output_dir=str(tmp_path),
        title="Song",
        artist="Artist",
        index=1,
        include_artist=False,
        playlist_name="Artist/Album: Deluxe",
    )

    assert found == expected


def test_hls_uses_the_same_clean_artist_title_stem_as_duplicate_lookup(
    tmp_path, monkeypatch,
):
    from core.downloader import DownloadEngine, DownloadStatus, MediaType
    from core.duplicate_checker import expected_stem

    def fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
        make_empty_audio(Path(output_path))

    monkeypatch.setattr("core.hls_downloader.download_hls", fake_download_hls)

    finished: list = []
    req = _album_request(
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
        audio_format="mp3",
        stream_type="hls",
        forced_title="Song: Name (Official Video)",
        forced_artist="Some/Artist",
        forced_index=None,
        filename_index=None,
        filename_include_artist=True,
        is_solo=True,
        on_progress=lambda p: finished.append(p) if p.status is DownloadStatus.FINISHED else None,
    )

    DownloadEngine()._download_hls_stream(req)

    written = Path(finished[0].output_path)
    expected = expected_stem(
        req.forced_title, req.forced_artist, include_artist=True,
    )
    assert written.stem == expected == "Some-Artist - Song - Name"


def test_hls_download_numbers_both_the_filename_and_the_tags(tmp_path, monkeypatch):
    """The raw-stream path never ran a metadata pass, so it numbered only the name.

    It also built "03 Title" while every other path (and
    core.duplicate_checker.expected_stem) uses "03 - Title".
    """
    from mutagen.id3 import ID3

    from core.downloader import DownloadEngine, DownloadStatus, MediaType
    from core.duplicate_checker import expected_stem

    def fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
        make_empty_audio(Path(output_path))

    monkeypatch.setattr("core.hls_downloader.download_hls", fake_download_hls)

    finished: list = []
    req = _album_request(
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
        audio_format="mp3",
        stream_type="hls",
        on_progress=lambda p: finished.append(p) if p.status is DownloadStatus.FINISHED else None,
    )
    DownloadEngine()._download_hls_stream(req)

    assert len(finished) == 1
    written = Path(finished[0].output_path)
    assert written.exists()
    assert written.stem == "03 - Track Title"
    # Same "NN - " prefix the duplicate checker looks for, so the file it
    # writes is the file a re-run can find.
    assert expected_stem("Track Title", "", 3, True, False).startswith("03 - ")
    assert str(ID3(str(written))["TRCK"]) == "3/12"
