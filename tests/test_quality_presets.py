from __future__ import annotations

import pytest


def test_audio_registry_order_and_ids_by_codec():
    from core.quality_presets import audio_presets_for_codec

    assert [p.id for p in audio_presets_for_codec("mp3")] == [
        "audio_mp3_320", "audio_mp3_256", "audio_mp3_192", "audio_mp3_128",
    ]
    assert [p.id for p in audio_presets_for_codec("m4a")] == [
        "audio_m4a_best", "audio_m4a_256", "audio_m4a_192", "audio_m4a_128",
    ]
    assert [p.id for p in audio_presets_for_codec("opus")] == [
        "audio_opus_best", "audio_opus_192", "audio_opus_160",
        "audio_opus_128", "audio_opus_96",
    ]
    assert [p.id for p in audio_presets_for_codec("flac")] == ["audio_flac_source"]


def test_video_registry_order_and_ids():
    from core.quality_presets import VIDEO_QUALITY_PRESETS

    assert [p.id for p in VIDEO_QUALITY_PRESETS] == [
        "video_best", "video_2160", "video_1440", "video_1080",
        "video_720", "video_480", "video_360", "video_smallest",
    ]


@pytest.mark.parametrize("preset_id, preferredquality", [
    ("audio_mp3_320", "320"),
    ("audio_mp3_256", "256"),
    ("audio_mp3_192", "192"),
    ("audio_mp3_128", "128"),
])
def test_mp3_presets_request_exact_bitrates(preset_id, preferredquality):
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import audio_quality_from_id

    req = DownloadRequest(
        url="https://example.com/audio",
        output_dir=".",
        media_type=MediaType.AUDIO,
        audio_format="mp3",
        audio_quality=audio_quality_from_id(preset_id, "mp3"),
    )
    opts = DownloadEngine._audio_opts(req)
    extractor = opts["postprocessors"][0]
    assert extractor["preferredcodec"] == "mp3"
    assert extractor["preferredquality"] == preferredquality


@pytest.mark.parametrize("codec, preset_id, preferredquality", [
    ("m4a", "audio_m4a_best", "0"),
    ("m4a", "audio_m4a_256", "256"),
    ("m4a", "audio_m4a_192", "192"),
    ("m4a", "audio_m4a_128", "128"),
    ("opus", "audio_opus_best", "256"),
    ("opus", "audio_opus_192", "192"),
    ("opus", "audio_opus_160", "160"),
    ("opus", "audio_opus_128", "128"),
    ("opus", "audio_opus_96", "96"),
])
def test_m4a_and_opus_presets_map_to_codec_specific_preferredquality(
    codec,
    preset_id,
    preferredquality,
):
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import audio_quality_from_id

    req = DownloadRequest(
        url="https://example.com/audio",
        output_dir=".",
        media_type=MediaType.AUDIO,
        audio_format=codec,
        audio_quality=audio_quality_from_id(preset_id, codec),
    )
    extractor = DownloadEngine._audio_opts(req)["postprocessors"][0]
    assert extractor["preferredcodec"] == codec
    assert extractor["preferredquality"] == preferredquality


def test_flac_source_quality_omits_preferredquality():
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import AudioQuality

    req = DownloadRequest(
        url="https://example.com/audio",
        output_dir=".",
        media_type=MediaType.AUDIO,
        audio_format="flac",
        audio_quality=AudioQuality.FLAC_SOURCE,
    )
    extractor = DownloadEngine._audio_opts(req)["postprocessors"][0]
    assert extractor["preferredcodec"] == "flac"
    assert "preferredquality" not in extractor


def test_invalid_audio_codec_quality_pair_fails_explicitly():
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import AudioQuality

    req = DownloadRequest(
        url="https://example.com/audio",
        output_dir=".",
        media_type=MediaType.AUDIO,
        audio_format="m4a",
        audio_quality=AudioQuality.MP3_320,
    )
    with pytest.raises(ValueError, match="audio_mp3_320"):
        DownloadEngine._audio_opts(req)


@pytest.mark.parametrize("quality_id, selector", [
    ("video_2160", "bestvideo[height<=2160]+bestaudio/best[height<=2160]"),
    ("video_1440", "bestvideo[height<=1440]+bestaudio/best[height<=1440]"),
    ("video_1080", "bestvideo[height<=1080]+bestaudio/best[height<=1080]"),
    ("video_720", "bestvideo[height<=720]+bestaudio/best[height<=720]"),
    ("video_480", "bestvideo[height<=480]+bestaudio/best[height<=480]"),
    ("video_360", "bestvideo[height<=360]+bestaudio/best[height<=360]"),
])
def test_resolution_video_presets_are_explicit_caps(quality_id, selector):
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import video_quality_from_id

    req = DownloadRequest(
        url="https://example.com/video",
        output_dir=".",
        media_type=MediaType.VIDEO,
        video_quality=video_quality_from_id(quality_id),
    )
    assert DownloadEngine._video_opts(req)["format"] == selector


def test_video_auto_is_unrestricted_best_available():
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import VideoQuality

    req = DownloadRequest(
        url="https://example.com/video",
        output_dir=".",
        media_type=MediaType.VIDEO,
        video_quality=VideoQuality.BEST,
    )
    opts = DownloadEngine._video_opts(req)
    assert opts["format"] == "bestvideo+bestaudio/best"
    assert "height<=" not in opts["format"]
    assert "format_sort" not in opts


def test_video_smallest_uses_format_sort_not_worst_selector():
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import VideoQuality

    req = DownloadRequest(
        url="https://example.com/video",
        output_dir=".",
        media_type=MediaType.VIDEO,
        video_quality=VideoQuality.SMALLEST,
    )
    opts = DownloadEngine._video_opts(req)
    assert opts["format"] == "best"
    assert opts["format_sort"] == ["+size", "+br", "+res", "+fps"]
    assert "worst" not in opts["format"]


def test_english_and_hebrew_labels_share_stable_ids():
    from core.quality_presets import AUDIO_QUALITY_PRESETS, VIDEO_QUALITY_PRESETS
    from ui.i18n import current_language, set_language, t

    ids_before = [p.id for p in AUDIO_QUALITY_PRESETS] + [p.id for p in VIDEO_QUALITY_PRESETS]
    previous_language = current_language()
    try:
        set_language("en")
        assert t("quality_best") == "Best"
        assert t("quality_video_full_hd") == "Full HD"
        set_language("he")
        assert t("quality_best") == "הכי טובה"
        assert t("quality_auto") == "אוטומטי"
    finally:
        set_language(previous_language)

    ids_after = [p.id for p in AUDIO_QUALITY_PRESETS] + [p.id for p in VIDEO_QUALITY_PRESETS]
    assert ids_after == ids_before


# ──────────────────────────────────────────────────────────────────────────────
# core/media_formats.py — centralized supported-format registry
# ──────────────────────────────────────────────────────────────────────────────

def test_media_formats_registry():
    from core.media_formats import (
        AUDIO_FORMATS,
        DEFAULT_AUDIO_FORMAT,
        DEFAULT_VIDEO_FORMAT,
        VIDEO_FORMATS,
        is_valid_audio_format,
        is_valid_video_format,
    )

    assert AUDIO_FORMATS == ("mp3", "m4a", "flac", "opus")
    assert VIDEO_FORMATS == ("mp4",)
    assert DEFAULT_AUDIO_FORMAT == "mp3"
    assert DEFAULT_VIDEO_FORMAT == "mp4"
    assert is_valid_audio_format("mp3") is True
    assert is_valid_audio_format("MP3") is True
    assert is_valid_audio_format("wav") is False
    assert is_valid_video_format("mp4") is True
    assert is_valid_video_format("mkv") is False


def test_video_opts_defaults_output_format_to_mp4():
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import VideoQuality

    req = DownloadRequest(
        url="https://example.com/video",
        output_dir=".",
        media_type=MediaType.VIDEO,
        video_quality=VideoQuality.P1080,
    )
    opts = DownloadEngine._video_opts(req)
    assert opts["merge_output_format"] == "mp4"
    assert opts["postprocessors"][0] == {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}


def test_video_opts_sources_output_format_from_request_not_a_literal():
    """merge_output_format/preferedformat must come from req.video_format,
    not a hardcoded "mp4" literal. Only "mp4" is exposed in the UI/config
    today (core/media_formats.py), but the engine plumbing itself must not
    assume it — proven here with a non-default value."""
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import VideoQuality

    req = DownloadRequest(
        url="https://example.com/video",
        output_dir=".",
        media_type=MediaType.VIDEO,
        video_quality=VideoQuality.P1080,
        video_format="mkv",
    )
    opts = DownloadEngine._video_opts(req)
    assert opts["merge_output_format"] == "mkv"
    assert opts["postprocessors"][0]["preferedformat"] == "mkv"


def test_download_hls_stream_uses_request_format_for_extension(monkeypatch, tmp_path):
    from core.downloader import DownloadEngine, DownloadRequest, MediaType
    from core.quality_presets import AudioQuality, VideoQuality

    captured: dict = {}

    def fake_download_hls(url, output_path, cookies_file=None, cancel_event=None):
        captured["output_path"] = output_path

    monkeypatch.setattr("core.hls_downloader.download_hls", fake_download_hls)

    engine = DownloadEngine()

    audio_req = DownloadRequest(
        url="https://example.com/stream.m3u8",
        output_dir=str(tmp_path),
        media_type=MediaType.AUDIO,
        audio_format="flac",
        audio_quality=AudioQuality.FLAC_SOURCE,
        stream_type="hls",
        forced_title="track",
    )
    engine._download_hls_stream(audio_req)
    assert captured["output_path"].endswith(".flac")

    video_req = DownloadRequest(
        url="https://example.com/stream.m3u8",
        output_dir=str(tmp_path),
        media_type=MediaType.VIDEO,
        video_quality=VideoQuality.P1080,
        video_format="mp4",
        stream_type="hls",
        forced_title="clip",
    )
    engine._download_hls_stream(video_req)
    assert captured["output_path"].endswith(".mp4")
