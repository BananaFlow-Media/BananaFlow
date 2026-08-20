"""Security floor guard for YouTube Doctor."""

from core.youtube_doctor import MIN_YT_DLP_VERSION, _parse_calver, check_yt_dlp_version


def test_doctor_minimum_is_security_patched_release_or_newer():
    assert _parse_calver(MIN_YT_DLP_VERSION) >= (2026, 7, 4)


def test_doctor_warns_for_last_known_vulnerable_baseline():
    check = check_yt_dlp_version("2026.6.9")
    assert check.status.value == "warn"


def test_doctor_accepts_first_security_patched_release():
    check = check_yt_dlp_version("2026.7.4")
    assert check.status.value == "pass"
