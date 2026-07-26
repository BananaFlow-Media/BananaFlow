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

    def test_records_had_pending_resolver_flag(self):
        """Finding #5: a job paused before its Spotify two-stage match ever
        ran carries a live url_resolver closure, which cannot be persisted
        -- but the caller still needs to know it was there, so it knows to
        rebuild an equivalent one on restore instead of trying to download
        req.url literally (still just a placeholder in this state)."""
        pending = DownloadRequest(url="placeholder", output_dir="/out", media_type=MediaType.AUDIO)
        pending.url_resolver = lambda ev: "https://resolved"
        assert request_to_dict(pending)["had_pending_resolver"] is True

        resolved = DownloadRequest(url="https://resolved", output_dir="/out", media_type=MediaType.AUDIO)
        assert request_to_dict(resolved)["had_pending_resolver"] is False

    def test_post_download_resume_checkpoint_survives_a_restart(self):
        """A job paused during post-processing has already downloaded every
        byte. Without the phase and the workspace file identity, the restored
        request re-runs yt-dlp against an already-complete file, no
        postprocessor hook fires, and the resume dies with "output file is
        missing"."""
        req = DownloadRequest(
            url="https://x", output_dir="/out", media_type=MediaType.AUDIO,
            workspace_dir="/ws/batch-1/jobA",
        )
        req.resume_phase = "postprocess"
        req.resume_final_path = "/ws/batch-1/jobA/Song.mp3"

        rebuilt = request_from_dict(request_to_dict(req))

        assert rebuilt.resume_phase == "postprocess"
        assert rebuilt.resume_final_path == "/ws/batch-1/jobA/Song.mp3"

    def test_a_request_with_no_checkpoint_round_trips_as_none(self):
        req = DownloadRequest(url="https://x", output_dir="/out", media_type=MediaType.AUDIO)
        rebuilt = request_from_dict(request_to_dict(req))
        assert rebuilt.resume_phase is None
        assert rebuilt.resume_final_path is None


# ── Store ────────────────────────────────────────────────────────────────────

def _job(key="a", ws="/ws/batch-1/keyA"):
    return PausedJob(
        key=key,
        request={"url": f"http://{key}", "output_dir": "/out", "workspace_dir": ws},
        card={"title": key.upper()},
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
        """load() stays lenient for ordinary pause/resume callers: whatever
        is still readable is returned rather than nothing at all. (The
        destructive sweep uses workspace_dirs_or_none instead, which does
        NOT tolerate this -- see
        test_workspace_dirs_or_none_is_none_when_a_single_record_is_malformed.)"""
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

    def test_workspace_dirs_or_none_is_empty_list_for_missing_file(self, tmp_path):
        """Genuinely nothing paused -- safe for the sweep to treat as an
        empty keep-set."""
        store = PausedBatchStore(tmp_path / "nope" / "paused.json")
        assert store.workspace_dirs_or_none() == []

    def test_workspace_dirs_or_none_is_empty_list_for_valid_empty_batch(self, tmp_path):
        store = PausedBatchStore(tmp_path / "paused.json")
        store.save([])
        assert store.workspace_dirs_or_none() == []

    def test_workspace_dirs_or_none_returns_the_keep_set_normally(self, tmp_path):
        store = PausedBatchStore(tmp_path / "paused.json")
        store.save([_job("a"), _job("b", "/ws/batch-1/keyB")])
        assert set(store.workspace_dirs_or_none()) == {"/ws/batch-1/keyA", "/ws/batch-1/keyB"}

    def test_workspace_dirs_or_none_is_none_for_corrupt_file(self, tmp_path):
        """A corrupt file means the keep-set is UNKNOWABLE, not empty -- the
        startup sweep must be able to tell the two apart, or it will delete
        every still-resumable workspace on disk just because the one record
        protecting them failed to parse."""
        p = tmp_path / "paused.json"
        p.write_text("{ not valid json ", encoding="utf-8")
        assert PausedBatchStore(p).workspace_dirs_or_none() is None

    def test_workspace_dirs_or_none_is_none_when_a_single_record_is_malformed(
        self, tmp_path,
    ):
        """A PARTIAL keep-set is as dangerous as an empty one: the entries
        missing from it are exactly the workspaces the sweep then deletes.
        A syntactically valid file with one unreadable record used to be
        reported as perfectly readable, so that record's still-resumable
        workspace was quietly left out of the keep-set and swept."""
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": [
            {"request": {"url": "u", "workspace_dir": "/ws/batch-1/ok"}},
            {"request": "not-a-dict"},   # one bad record among good ones
        ]}), encoding="utf-8")
        assert PausedBatchStore(p).workspace_dirs_or_none() is None

    def test_a_record_with_no_workspace_is_unusable_not_merely_empty(self, tmp_path):
        """A paused job IS its workspace: the .part file, the intermediates
        and any already-finished output all live there, and every job the
        orchestrator can hand out for a pause has one by construction. A
        record without one cannot be resumed from AND contributes nothing to
        the keep-set, so whatever workspace that job really had on disk was
        left unprotected and swept. It counts as unreadable."""
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": [
            {"request": {"url": "u", "output_dir": "/out"}},          # no workspace
            {"request": {"url": "v", "workspace_dir": "/ws/batch-1/ok"}},
        ]}), encoding="utf-8")

        store = PausedBatchStore(p)
        assert store.workspace_dirs_or_none() is None
        # It is also not offered up as something resumable.
        assert [j.workspace_dir for j in store.load()] == ["/ws/batch-1/ok"]

    def test_a_blank_workspace_string_counts_the_same(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": [
            {"request": {"url": "u", "workspace_dir": "   "}},
        ]}), encoding="utf-8")
        assert PausedBatchStore(p).workspace_dirs_or_none() is None
        assert PausedBatchStore(p).load() == []

    def test_an_unreadable_file_is_not_the_same_as_a_missing_one(self, tmp_path, monkeypatch):
        """Only FileNotFoundError means "nothing was ever paused". A
        permission error, a lock, or a transient I/O failure on a network
        profile means the state is UNKNOWN — reporting it as empty handed
        the startup sweep an empty keep-set and let one bad read delete
        every workspace on disk."""
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": []}), encoding="utf-8")

        def _boom(*_a, **_k):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(type(p), "read_text", _boom)

        store = PausedBatchStore(p)
        assert store.workspace_dirs_or_none() is None
        assert store.load() == []      # still never raises

    def test_undecodable_bytes_are_reported_as_unreadable(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_bytes(b"\xff\xfe\x00\x00not utf-8 at all \xc3\x28")
        assert PausedBatchStore(p).workspace_dirs_or_none() is None
        assert PausedBatchStore(p).load() == []

    def test_a_genuinely_missing_file_is_still_an_empty_keep_set(self, tmp_path):
        """The other half: a first run with nothing ever paused must NOT
        disable the sweep, or stale workspaces would never be reclaimed."""
        store = PausedBatchStore(tmp_path / "never" / "written.json")
        assert store.workspace_dirs_or_none() == []

    def test_unexpected_shape_is_reported_as_unreadable(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": "not-a-list"}), encoding="utf-8")
        assert PausedBatchStore(p).workspace_dirs_or_none() is None
        # load() itself keeps its simpler "corrupt == empty" contract for
        # ordinary (non-destructive) callers.
        assert PausedBatchStore(p).load() == []

    def test_workspace_dirs_or_none_is_none_for_wrong_shaped_payload(self, tmp_path):
        p = tmp_path / "paused.json"
        p.write_text(json.dumps({"jobs": "not-a-list"}), encoding="utf-8")
        assert PausedBatchStore(p).workspace_dirs_or_none() is None


class TestPausedJobWorkspaceDir:
    def test_derived_from_the_nested_request_field(self):
        """Finding #8: workspace_dir must come from exactly one place."""
        job = PausedJob(
            key="a",
            request={"url": "u", "output_dir": "o", "workspace_dir": "/ws/batch-1/a"},
            card={},
        )
        assert job.workspace_dir == "/ws/batch-1/a"

    def test_ignores_a_stale_top_level_copy_from_an_older_file_format(self):
        """An older persisted file may still have a top-level "workspace_dir"
        key (the format this fix retires) alongside the nested one -- if
        the two ever disagree, the nested one (the one the request is
        actually rebuilt from) must always win, never the leftover
        top-level copy nothing reconstructs the request from."""
        job = PausedJob.from_dict({
            "key": "a",
            "request": {"url": "u", "workspace_dir": "/ws/batch-1/real"},
            "card": {},
            "workspace_dir": "/ws/batch-1/stale-and-wrong",
        })
        assert job is not None
        assert job.workspace_dir == "/ws/batch-1/real"

    def test_missing_workspace_dir_is_empty_string(self):
        job = PausedJob(key="a", request={"url": "u"}, card={})
        assert job.workspace_dir == ""
