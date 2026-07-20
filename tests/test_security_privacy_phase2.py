"""Phase 2 regression coverage for credentials, redaction, and safe output."""

from __future__ import annotations

import ast
import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.security import REDACTED, RedactingFormatter, redact_data, redact_text


def _realistic_google_key() -> str:
    return "AI" + "za" + "A" * 35


def _realistic_github_token() -> str:
    return "gh" + "p_" + "B" * 36


def _realistic_jwt() -> str:
    return ".".join(("ey" + "Jheader", "ey" + "Jpayload", "signature_value"))


@pytest.mark.parametrize(
    "template, secret",
    [
        ("Authorization: Bearer {secret}", "bearer-value.abc123"),
        ("Cookie: SID={secret}; LOGIN_INFO=other", "cookie-value"),
        ("https://example.test/path?api_key={secret}&page=2", "query-value"),
        ("https://user:{secret}@proxy.example:8443/path", "proxy-password"),
        ("X-App-Token: {secret}", "proxy-token"),
        ("service returned {secret}", _realistic_google_key()),
        ("service returned {secret}", _realistic_github_token()),
        ("service returned {secret}", _realistic_jwt()),
    ],
)
def test_redact_text_covers_realistic_secret_shapes(template, secret):
    safe = redact_text(template.format(secret=secret))
    assert secret not in safe
    assert REDACTED in safe


def test_redact_text_hides_netscape_cookie_values():
    value = "session-cookie-material"
    line = f".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\t{value}"
    safe = redact_text(line)
    assert value not in safe
    assert safe.endswith(REDACTED)


def test_redact_data_handles_nested_structures_and_empty_defaults():
    data = {
        "headers": {"Authorization": "Bearer nested-secret"},
        "items": [{"client_secret": "client-secret"}, {"api_key": ""}],  # pragma: allowlist secret
        "cookies_file": "C:/private/cookies.txt",
        "normal": "visible",
    }
    safe = redact_data(data)
    assert safe["headers"]["Authorization"] == REDACTED
    assert safe["items"][0]["client_secret"] == REDACTED
    assert safe["items"][1]["api_key"] == ""
    assert safe["cookies_file"] == REDACTED
    assert safe["normal"] == "visible"


def test_redacting_formatter_protects_message_arguments_and_tracebacks():
    secret = "traceback-cookie-value"  # pragma: allowlist secret
    try:
        raise RuntimeError(f"Cookie: SID={secret}")
    except RuntimeError:
        record = logging.LogRecord(
            "phase2", logging.ERROR, __file__, 1,
            "request failed: %s", (f"Authorization: Bearer {secret}",),
            exc_info=__import__("sys").exc_info(),
        )
    rendered = RedactingFormatter("%(levelname)s %(message)s").format(record)
    assert secret not in rendered
    assert rendered.count(REDACTED) >= 2


def test_error_info_never_carries_raw_secret():
    from error_handler import classify_error

    secret = "error-path-access-token"  # pragma: allowlist secret
    info = classify_error(RuntimeError(f"request failed?access_token={secret}"))
    assert secret not in info.raw
    assert secret not in info.detail
    assert REDACTED in info.raw


def test_hls_debug_log_never_contains_cookie_or_sensitive_url(
    tmp_path, monkeypatch, caplog,
):
    from core import hls_downloader

    cookie_value = "hls-session-cookie"
    query_value = "signed-query-value"
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t" + cookie_value + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.mp4"
    output.write_bytes(b"media")
    monkeypatch.setattr(hls_downloader, "_find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(
        hls_downloader.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    caplog.set_level(logging.DEBUG, logger=hls_downloader.__name__)
    hls_downloader.download_hls(
        f"https://cdn.example/stream.m3u8?token={query_value}",
        str(output),
        cookies_file=str(cookies),
    )
    assert cookie_value not in caplog.text
    assert query_value not in caplog.text
    assert "Cookie: SID=" not in caplog.text


def test_secret_defaults_are_empty_and_sources_have_no_committed_fallback():
    root = Path(__file__).resolve().parents[1]
    config_tree = ast.parse((root / "config.py").read_text(encoding="utf-8"))
    defaults: dict[str, ast.expr] = {}
    for node in ast.walk(config_tree):
        is_defaults = (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_DEFAULTS"
        ) or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_DEFAULTS"
                for target in node.targets
            )
        )
        if is_defaults and isinstance(node.value, ast.Dict):
            defaults = {
                key.value: value
                for key, value in zip(node.value.keys, node.value.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            break

    for key in ("spotify_client_id", "spotify_client_secret", "spotify_app_api_key"):
        assert isinstance(defaults[key], ast.Constant)
        assert defaults[key].value == ""

    ytm_source = (root / "utils" / "ytm_scraper.py").read_text(encoding="utf-8")
    assert _realistic_google_key() not in ytm_source
    assert 'os.environ.get("YOUTUBE_API_KEY", "")' in ytm_source


def test_config_environment_overrides_and_safe_export(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    env_secret = "environment-only-secret"  # pragma: allowlist secret
    monkeypatch.setenv("BANANAFLOW_SPOTIFY_APP_API_KEY", env_secret)
    from config import AppConfig

    cfg = AppConfig()
    cfg.spotify_app_api_key = "stored-secret"  # pragma: allowlist secret
    assert cfg.spotify_app_api_key == env_secret
    assert cfg.as_dict()["spotify_app_api_key"] == REDACTED
    assert cfg.as_dict(include_secrets=True)["spotify_app_api_key"] == "stored-secret"  # pragma: allowlist secret
    assert env_secret not in repr(cfg.as_dict(include_secrets=True))


def test_migration_removes_only_the_retired_default(monkeypatch):
    import config_migrate

    retired = "synthetic-retired-default"
    monkeypatch.setattr(
        config_migrate,
        "_RETIRED_SPOTIFY_DEFAULT_SHA256",
        hashlib.sha256(retired.encode("utf-8")).hexdigest(),
    )
    old = {"config_version": 8, "spotify_app_api_key": retired}
    custom = {  # pragma: allowlist secret
        "config_version": 8,
        "spotify_app_api_key": "user-owned-value",  # pragma: allowlist secret
    }
    assert config_migrate.migrate(old) is True
    assert old["spotify_app_api_key"] == ""
    assert config_migrate.migrate(custom) is True
    assert custom["spotify_app_api_key"] == "user-owned-value"  # pragma: allowlist secret
