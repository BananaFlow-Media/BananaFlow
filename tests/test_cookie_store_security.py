"""Security boundary tests for BananaFlow-owned authentication cookies."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


COOKIE_HEADER = "# Netscape HTTP Cookie File\n"


def _line(domain: str, name: str, value: str, expiry: int = 4_102_444_800) -> str:
    return f"{domain}\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}\n"


def _auth_text(secret: str = "session-secret") -> str:
    return (
        COOKIE_HEADER
        + _line(".youtube.com", "LOGIN_INFO", secret)
        + _line(".youtube.com", "SAPISID", "sapisid-secret")
        + _line(".youtube.com", "VISITOR_INFO1_LIVE", "visitor-secret")
    )


def test_scoped_store_keeps_only_required_youtube_auth_cookies():
    from utils.cookie_store import scoped_cookie_text

    raw = (
        _auth_text()
        + _line(".google.com", "NID", "unrelated-google-account-cookie")
        + _line(".google.com", "AEC", "unrelated-anti-abuse-cookie")
        + _line(".example.com", "LOGIN_INFO", "wrong-domain")
        + _line(".youtube.com", "LOGIN_INFO", "expired", expiry=1)
    )
    stored = scoped_cookie_text(raw)
    assert "session-secret" in stored
    assert "sapisid-secret" in stored
    assert "visitor-secret" in stored
    assert "unrelated-google-account-cookie" not in stored
    assert "unrelated-anti-abuse-cookie" not in stored
    assert "wrong-domain" not in stored
    assert "\texpired\n" not in stored


def test_windows_store_is_encrypted_and_materialized_only_temporarily(
    tmp_path, monkeypatch,
):
    import utils.cookie_store as store

    monkeypatch.setattr(store, "_WINDOWS", True)
    monkeypatch.setattr(store, "_protect_current_user", lambda data: b"protected:" + data[::-1])
    monkeypatch.setattr(
        store, "_unprotect_current_user",
        lambda data: data.removeprefix(b"protected:")[::-1],
    )
    monkeypatch.setattr(store, "_private_temp_dir", lambda: tmp_path / "auth-temp")
    destination = tmp_path / "cookies.dpapi"
    secret = "never-plaintext-at-rest"

    store.write_cookie_store(destination, _auth_text(secret))
    at_rest = destination.read_bytes()
    assert secret.encode() not in at_rest
    assert at_rest.startswith(store.DPAPI_MAGIC)

    materialized = None
    with store.materialize_cookie_file(destination) as temporary:
        materialized = Path(temporary)
        assert materialized.exists()
        assert secret in materialized.read_text(encoding="utf-8")
    assert materialized is not None
    assert not materialized.exists()


def test_materialization_cleanup_runs_when_consumer_raises(tmp_path, monkeypatch):
    import utils.cookie_store as store

    monkeypatch.setattr(store, "_WINDOWS", False)
    monkeypatch.setattr(store, "_private_temp_dir", lambda: tmp_path / "auth-temp")
    source = tmp_path / "cookies.txt"
    source.write_text(_auth_text(), encoding="utf-8")
    materialized = None
    with pytest.raises(RuntimeError, match="consumer failed"):
        with store.materialize_cookie_file(source) as temporary:
            materialized = Path(temporary)
            raise RuntimeError("consumer failed")
    assert materialized is not None
    assert not materialized.exists()


def test_cookie_values_and_temporary_paths_never_reach_logs(
    tmp_path, monkeypatch, caplog,
):
    import utils.cookie_store as store

    monkeypatch.setattr(store, "_WINDOWS", False)
    monkeypatch.setattr(store, "_private_temp_dir", lambda: tmp_path / "private-auth-temp")
    source = tmp_path / "cookies.txt"
    secret = "super-secret-cookie-value"
    source.write_text(_auth_text(secret), encoding="utf-8")

    caplog.set_level(logging.DEBUG)
    with store.materialize_cookie_file(source) as temporary:
        temporary_path = str(temporary)

    rendered = caplog.text
    assert secret not in rendered
    assert temporary_path not in rendered


def test_legacy_app_cookie_migration_encrypts_then_removes_plaintext(
    tmp_path, monkeypatch,
):
    import utils.cookie_store as store

    legacy = tmp_path / "app_cookies.txt"
    protected = tmp_path / "app_cookies.dpapi"
    legacy.write_text(_auth_text("legacy-secret"), encoding="utf-8")
    monkeypatch.setattr(store, "_WINDOWS", True)
    monkeypatch.setattr(store, "_protect_current_user", lambda data: b"protected:" + data[::-1])
    monkeypatch.setattr(
        store, "_unprotect_current_user",
        lambda data: data.removeprefix(b"protected:")[::-1],
    )
    monkeypatch.setattr(store, "get_legacy_app_cookies_path", lambda: legacy)
    monkeypatch.setattr(store, "get_app_cookies_path", lambda: protected)

    migrated = store.migrate_legacy_app_cookie_store(str(legacy))
    assert migrated == str(protected)
    assert protected.exists()
    assert b"legacy-secret" not in protected.read_bytes()
    assert not legacy.exists()
    assert "legacy-secret" in store.read_cookie_store(protected)


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="real DPAPI is Windows-only")
def test_real_dpapi_round_trip_is_current_user_scoped():
    from utils.cookie_store import _protect_current_user, _unprotect_current_user

    plaintext = b"bananaflow-real-dpapi-round-trip"
    encrypted = _protect_current_user(plaintext)
    assert encrypted != plaintext
    assert _unprotect_current_user(encrypted) == plaintext
