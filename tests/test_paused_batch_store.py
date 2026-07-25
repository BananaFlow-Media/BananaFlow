"""
tests/test_paused_batch_store.py  –  Persisted paused-download state
========================================================================
Covers the authoritative persistence layer for pause/resume across restart:

  * core.download_request_codec  – serialise/rebuild a resumable request,
    tolerant of missing/malformed fields.
  * core.paused_batch_store      – save/load/clear, corrupt-file-safe,
    atomic write.

Pure stdlib + core, no Qt.
"""

from __future__ import annotations

import json

import pytest

from core.downloader import DownloadRequest, MediaType
from core.playlist_parser import SourcePlatform
from core.download_request_codec import request_from_dict, request_to_dict
from core.paused_batch_store import PausedBatchStore, PausedJob


# ── Codec ────────────────────────────────────────────────────────────────────

class TestRequestCodec:
    def test_roundtrip_preserves_resumable_fields(self):
        req = DownloadRequest(
            url="http://x", output_dir="/out", workspace_dir="/ws/batch/k",
            media_type=MediaType.AUDIO, forced_title="T", forced_album="Alb",
            forced_artist="Artist", forced_duration=210, thumbnail_url="http://t",
            platform=SourcePlatform.YOUTUBE, category="stream:hls", is_solo=True,
            square_thumbnails=True, expand_thumbnails=True, embed_lyrics=True,
            cookies_browser="chrome", proxy_url="http://proxy", playlist_name="Album",
        )
        d = request_to_dict(req)
        # Must be JSON-serialisable (no enums/events leaked through).
        json.dumps(d)

        r2 = request_from_dict(json.loads(json.dumps(d)))
        assert r2.workspace_dir == "/ws/batch/k"
        assert r2.forced_album == "Alb"
        assert r2.forced_duration == 210
        assert r2.thumbnail_url == "http://t"
        assert r2.platform == SourcePlatform.YOUTUBE
        assert r2.category == "stream:hls"
        assert r2.is_solo is True
        assert r2.square_thumbnails is True
        assert r2.cookies_browser == "chrome"
        assert r2.proxy_url == "http://proxy"
        assert r2.playlist_name == "Album"
        assert r2.media_type == MediaType.AUDIO
        assert r2.resumable is True

    def test_transient_state_is_reset(self):
        req = DownloadRequest(url="u", output_dir="o", media_type=MediaType.AUDIO)
        r2 = request_from_dict(request_to_dict(req))
        assert r2.cancel_event is None
        assert r2.on_progress is None and r2.on_finished is None and r2.on_error is None
        assert r2._final_output_path == ""  # noqa: SLF001

    def test_missing_required_fields_do_not_raise(self):
        r = request_from_dict({})  # totally empty
        assert r.url == ""
        assert r.output_dir == ""
        assert r.resumable is True

    def test_unknown_enum_values_fall_back_safely(self):
        r = request_from_dict({
            "url": "u", "output_dir": "o",
            "media_type": "BOGUS", "audio_quality": "BOGUS",
            "video_quality": "BOGUS", "platform": "BOGUS",
        })
        assert r.media_type == MediaType.AUDIO
        assert r.platform is None


# ── Store ────────────────────────────────────────────────────────────────────

def _job(key="a", ws="/ws/batch-1/keyA"):
    return PausedJob(
        key=key,
        request={"url": f"http://{key}", "output_dir": "/out", "workspace_dir": ws},
        card={"title": key.upper()},
        workspace_dir=ws,
    )


class TestPausedBatchStore:
    def test_missing_file_loads_empty(self, tmp_path):
        store = PausedBatchStore(tmp_path / "nope" / "paused.json")
        assert store.load() == []

    def test_save_then_load_roundtrip(self, tmp_path):
        store = PausedBatchStore(tmp_path / "paused.json")
        store.save([_job("a"), _job("b", "/ws/batch-1/keyB")])
        loaded = store.load()
        assert [j.key for j in loaded] == ["a", "b"]
        assert loaded[0].card == {"title": "A"}
        assert set(store.workspace_dirs()) == {"/ws/batch-1/keyA", "/ws/batch-1/keyB"}

    def test_corrupt_file_loads_empty(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_text("{ not valid json ", encoding="utf-8")
        assert PausedBatchStore(p).load() == []

    def test_empty_file_loads_empty(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_text("   ", encoding="utf-8")
        assert PausedBatchStore(p).load() == []

    def test_partial_records_are_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": [
            {"request": {"url": "u", "workspace_dir": "/ws/batch-1/ok"}},  # valid
            "garbage-string",                                              # invalid
            {"no_request_key": True},                                     # invalid
            {"request": "not-a-dict"},                                    # invalid
        ]}), encoding="utf-8")
        jobs = PausedBatchStore(p).load()
        assert len(jobs) == 1
        assert jobs[0].workspace_dir == "/ws/batch-1/ok"

    def test_clear_removes_the_file(self, tmp_path):
        store = PausedBatchStore(tmp_path / "paused.json")
        store.save([_job()])
        assert store.path.exists()
        store.clear()
        assert not store.path.exists()
        assert store.load() == []

    def test_clear_missing_file_is_safe(self, tmp_path):
        PausedBatchStore(tmp_path / "paused.json").clear()  # must not raise

    def test_save_is_atomic_no_tmp_leftover(self, tmp_path):
        store = PausedBatchStore(tmp_path / "paused.json")
        store.save([_job()])
        leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []

    def test_top_level_list_form_is_accepted(self, tmp_path):
        """A bare list payload (not wrapped in {'jobs': ...}) still loads."""
        p = tmp_path / "paused.json"
        p.write_text(json.dumps([
            {"request": {"url": "u", "workspace_dir": "/ws/batch-1/x"}},
        ]), encoding="utf-8")
        jobs = PausedBatchStore(p).load()
        assert len(jobs) == 1
