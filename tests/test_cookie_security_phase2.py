"""Phase 2 cookie storage, expiry, deletion, and permission regressions."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.cookie_validator import check_cookies_valid, merge_cookies_file
from utils.security import delete_stored_auth_data, write_private_text


def _cookie_line(name: str, value: str, expiry: int = 0) -> str:
    return f".youtube.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}"


def test_private_cookie_write_is_atomic_and_owner_only(tmp_path):
    destination = tmp_path / "auth" / "cookies.txt"
    write_private_text(destination, _cookie_line("LOGIN_INFO", "private-value") + "\n")
    assert destination.exists()
    assert not list(destination.parent.glob(".*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL command is platform-specific")
def test_windows_acl_hardening_uses_current_account(tmp_path, monkeypatch):
    from utils import proc, security

    target = tmp_path / "cookies.txt"
    target.write_text("test", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run_hidden(command, *, purpose, **_kwargs):
        calls.append(list(command))
        return proc.ProcessResult(
            purpose=purpose, program="icacls", returncode=0,
        )

    monkeypatch.setattr(proc, "run_hidden", fake_run_hidden)
    monkeypatch.setattr(security, "_CACHED_PRINCIPAL", "*S-1-5-21-99-99-99-1001")
    security.restrict_path_permissions(target)

    icacls = calls[-1]
    assert icacls[0] == "icacls"
    assert "/inheritance:r" in icacls
    assert "*S-1-5-21-99-99-99-1001:(F)" in icacls


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL command is platform-specific")
def test_acl_hardening_starts_no_extra_process_to_identify_the_user(monkeypatch):
    """The principal must come from the process token, not from ``whoami``.

    Every ``whoami.exe`` launch from a windowed build flashes a console
    window, and hardening runs on startup and on every config save.
    """
    from utils import security

    monkeypatch.setattr(security, "_CACHED_PRINCIPAL", None)

    def fail(*_args, **_kwargs):
        raise AssertionError("ACL hardening must not spawn a child process")

    monkeypatch.setattr(security.subprocess, "run", fail)
    principal = security._acl_principal()

    assert principal.startswith("*S-1-")


def test_acl_principal_is_resolved_once_per_process(monkeypatch):
    """Repeated hardening must not repeat the identity lookup."""
    from utils import security

    monkeypatch.setattr(security, "_CACHED_PRINCIPAL", None)
    calls = {"n": 0}

    def counting_sid():
        calls["n"] += 1
        return "*S-1-5-21-1-2-3-1001"

    monkeypatch.setattr(security, "_current_user_sid", counting_sid)
    assert security._acl_principal() == "*S-1-5-21-1-2-3-1001"
    assert security._acl_principal() == "*S-1-5-21-1-2-3-1001"
    assert calls["n"] == 1


def test_merge_cookies_replaces_matching_entry_and_discards_other_sites(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "stored.txt"
    source.write_text(
        _cookie_line("LOGIN_INFO", "new-login") + "\n",
        encoding="utf-8",
    )
    destination.write_text(
        _cookie_line("LOGIN_INFO", "old-login")
        + "\n.example.com\tTRUE\t/\tTRUE\t0\tOTHER\tkeep-me\n",
        encoding="utf-8",
    )
    merge_cookies_file(source, destination)
    stored = destination.read_text(encoding="utf-8")
    assert "new-login" in stored
    assert "old-login" not in stored
    assert "keep-me" not in stored


def test_http_only_login_cookie_is_preserved_and_validated(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tprivate\n",
        encoding="utf-8",
    )
    assert check_cookies_valid(cookie_file) == (True, "")
    destination = tmp_path / "stored.txt"
    merge_cookies_file(cookie_file, destination)
    assert "#HttpOnly_.youtube.com" in destination.read_text(encoding="utf-8")


def test_google_secure_login_cookie_is_accepted(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        ".google.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tprivate\n",
        encoding="utf-8",
    )
    assert check_cookies_valid(cookie_file) == (True, "")


def test_locked_cookie_destination_fails_without_configuring_partial_data(
    tmp_path, monkeypatch,
):
    from utils import cookie_validator

    source = tmp_path / "source.txt"
    destination = tmp_path / "stored.txt"
    source.write_text(_cookie_line("LOGIN_INFO", "new") + "\n", encoding="utf-8")
    destination.write_text(_cookie_line("LOGIN_INFO", "old") + "\n", encoding="utf-8")
    before = destination.read_bytes()

    def locked(*_args, **_kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(cookie_validator, "write_private_text", locked)
    with pytest.raises(PermissionError):
        merge_cookies_file(source, destination)
    assert destination.read_bytes() == before


def test_session_login_cookie_is_valid(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n" + _cookie_line("LOGIN_INFO", "session", 0) + "\n",
        encoding="utf-8",
    )
    assert check_cookies_valid(cookie_file) == (True, "")


def test_login_info_on_lookalike_domain_is_not_trusted(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    lookalike = "youtube.com.evil.example\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tspoof"
    cookie_file.write_text(
        "# Netscape HTTP Cookie File\n" + lookalike + "\n",
        encoding="utf-8",
    )
    ok, message = check_cookies_valid(cookie_file)
    assert ok is False
    assert message


def test_login_info_on_real_youtube_subdomain_is_trusted(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    line = "music.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tsession"
    cookie_file.write_text(line + "\n", encoding="utf-8")
    assert check_cookies_valid(cookie_file) == (True, "")


def test_all_expired_cookies_fail_safely(tmp_path):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(
        _cookie_line("LOGIN_INFO", "expired", 1) + "\n",
        encoding="utf-8",
    )
    ok, message = check_cookies_valid(cookie_file)
    assert ok is False
    assert message


def test_delete_auth_data_removes_only_app_owned_targets(tmp_path):
    cookie_file = tmp_path / "app_cookies.txt"
    profile = tmp_path / "browser_profile"
    external = tmp_path / "user-export.txt"
    cookie_file.write_text("private", encoding="utf-8")
    profile.mkdir()
    (profile / "Cookies").write_text("private", encoding="utf-8")
    external.write_text("must remain", encoding="utf-8")

    result = delete_stored_auth_data(cookie_path=cookie_file, profile_dir=profile)
    assert result.success
    assert set(result.removed) == {"cookies", "browser_profile"}
    assert not cookie_file.exists()
    assert not profile.exists()
    assert external.read_text(encoding="utf-8") == "must remain"


def test_delete_auth_data_reports_locked_profile_for_safe_retry(tmp_path, monkeypatch):
    from utils import security

    cookie_file = tmp_path / "app_cookies.txt"
    profile = tmp_path / "browser_profile"
    profile.mkdir()

    def locked(_path):
        raise PermissionError("locked")

    monkeypatch.setattr(security.shutil, "rmtree", locked)
    result = delete_stored_auth_data(cookie_path=cookie_file, profile_dir=profile)
    assert result.success is False
    assert result.failed == ("browser_profile",)
    assert profile.exists()


def test_cookie_wizard_uses_private_writer_and_profile_hardening():
    source = (Path(__file__).resolve().parents[1] / "core" / "cookie_wizard.py").read_text(
        encoding="utf-8"
    )
    assert "write_private_text(cookie_path, netscape_str)" in source
    assert "restrict_path_permissions(profile_dir, recursive=True)" in source
    assert "navigator.webdriver" not in source
    assert "ignore_default_args" not in source


def test_cookie_wizard_export_is_scoped_and_preserves_http_only_format():
    from core.cookie_wizard import format_netscape_cookies

    exported = format_netscape_cookies([
        {
            "domain": ".youtube.com",
            "path": "/",
            "secure": True,
            "expires": -1,
            "httpOnly": True,
            "name": "LOGIN_INFO",
            "value": "youtube-secret",
        },
        {
            "domain": ".google.com",
            "path": "/",
            "secure": True,
            "expires": 2_000_000_000,
            "httpOnly": False,
            "name": "__Secure-3PSID",
            "value": "google-secret",
        },
        {
            "domain": ".example.com",
            "path": "/",
            "secure": True,
            "expires": 2_000_000_000,
            "httpOnly": False,
            "name": "SESSION",
            "value": "unrelated-secret",
        },
    ])

    assert "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tyoutube-secret" in exported
    assert ".google.com\tTRUE\t/\tTRUE\t2000000000\t__Secure-3PSID\tgoogle-secret" in exported
    assert "example.com" not in exported
    assert "unrelated-secret" not in exported
