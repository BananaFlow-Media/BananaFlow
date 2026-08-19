"""Security policy: yt-dlp must keep HTTPS certificate verification enabled."""

from __future__ import annotations

from utils import yt_dlp_opts


def test_base_options_do_not_disable_certificate_verification(monkeypatch):
    monkeypatch.setattr(yt_dlp_opts, "_detect_js_runtimes", lambda: {})

    opts = yt_dlp_opts.build_base_ydl_opts(
        enable_po_token_provider=False,
        retries=1,
    )

    # yt-dlp's secure default is certificate verification ON. The opt-out
    # flag is deliberately absent rather than set False, so upstream remains
    # authoritative about its normal TLS verification path.
    assert "nocheckcertificate" not in opts
