"""YouTube player-client policy must stay scoped to authenticated sessions."""

from __future__ import annotations

from utils import yt_dlp_opts


def _stub_runtime_and_provider(monkeypatch):
    monkeypatch.setattr(yt_dlp_opts, "_detect_js_runtimes", lambda: {})
    monkeypatch.setattr(yt_dlp_opts, "po_token_provider_circuit_open", lambda: False)
    monkeypatch.setattr(
        yt_dlp_opts,
        "_detect_bundled_pot_provider_args",
        lambda: {"youtubepot-bgutilscript": {"server_home": ["C:/provider"]}},
    )
    monkeypatch.setattr(yt_dlp_opts, "install_bgutil_stderr_capture", lambda: None)


def test_public_downloads_leave_player_client_to_upstream(monkeypatch):
    _stub_runtime_and_provider(monkeypatch)

    opts = yt_dlp_opts.build_base_ydl_opts()

    assert "youtube" not in opts.get("extractor_args", {})
    assert "youtubepot-bgutilscript" in opts.get("extractor_args", {})


def test_cookie_file_adds_current_web_embedded_fallback_without_dropping_provider(monkeypatch):
    _stub_runtime_and_provider(monkeypatch)

    opts = yt_dlp_opts.build_base_ydl_opts(cookies_file="cookies.txt")

    assert opts["cookiefile"] == "cookies.txt"
    assert opts["extractor_args"]["youtube"]["player_client"] == [
        "default",
        "web_embedded",
    ]
    assert "youtubepot-bgutilscript" in opts["extractor_args"]
