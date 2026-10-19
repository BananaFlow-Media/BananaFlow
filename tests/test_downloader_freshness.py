"""Deterministic regression tests for the read-only upstream review signal."""
import json
from io import BytesIO
from urllib.error import URLError

import pytest

from scripts import check_downloader_freshness as freshness

PIN = "2026.8.30.232658.dev0"
NEW = "2026.10.10.233141.dev0"


def metadata(*versions):
    return {"releases": {v: [{"yanked": False}] for v in versions}}


@pytest.fixture
def setup_check(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text(f"yt-dlp[default]=={PIN}\n")
    monkeypatch.setattr(freshness, "ROOT", tmp_path)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    return tmp_path, summary


def serve(monkeypatch, data):
    def open_response(url, timeout):
        assert url == "https://pypi.org/pypi/yt-dlp/json"
        assert timeout == 20
        return BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(freshness.urllib.request, "urlopen", open_response)


def test_new_candidate_is_visible_review_notice_not_failure(setup_check, monkeypatch, capsys):
    root, summary = setup_check
    before = (root / "requirements.txt").read_bytes()
    serve(monkeypatch, metadata(PIN, NEW))
    assert freshness.main() == 0
    assert "::warning" in capsys.readouterr().out
    assert "Human review required" in summary.read_text()
    assert NEW in summary.read_text() and PIN in summary.read_text()
    assert (root / "requirements.txt").read_bytes() == before
    assert sorted(p.name for p in root.iterdir()) == ["requirements.txt", "summary.md"]


def test_current_pin_has_no_warning(setup_check, monkeypatch, capsys):
    serve(monkeypatch, metadata(PIN))
    assert freshness.main() == 0
    assert "::warning" not in capsys.readouterr().out
    assert "Reviewed pin is current" in setup_check[1].read_text()


def test_uses_pep440_ordering_and_ignores_empty_and_yanked_releases():
    data = metadata(PIN, "2026.8.30.232658")
    data["releases"]["2099.1.1"] = [{"yanked": True}]
    data["releases"]["2099.1.2"] = []
    assert freshness.compare(f"yt-dlp[default]=={PIN}", data) == (PIN, "2026.8.30.232658")


def test_partially_yanked_release_is_still_a_candidate():
    data = metadata(PIN, NEW)
    data["releases"][NEW].append({"yanked": True})
    assert freshness.compare(f"yt-dlp[default]=={PIN}", data)[1] == NEW


@pytest.mark.parametrize("requirements", ["", "yt-dlp>=2026.8.19", "yt-dlp[default]==invalid",
    f"yt-dlp[default]=={PIN}\nyt-dlp[default]=={NEW}"])
def test_invalid_pin_remains_an_error(requirements):
    with pytest.raises(ValueError):
        freshness.compare(requirements, metadata(PIN, NEW))


@pytest.mark.parametrize("data", [{}, {"releases": []}, {"releases": {}},
    {"releases": {PIN: [{"yanked": True}]}}, {"releases": {PIN: [{}]}},
    {"releases": {PIN: "not files"}}, metadata(NEW), metadata(PIN, "invalid")])
def test_bad_metadata_or_missing_yanked_pin_remains_an_error(data):
    with pytest.raises((ValueError, KeyError)):
        freshness.compare(f"yt-dlp[default]=={PIN}", data)


def test_network_failure_is_not_reported_as_current(setup_check, monkeypatch):
    def fail(*args, **kwargs):
        raise URLError("test outage")
    monkeypatch.setattr(freshness.urllib.request, "urlopen", fail)
    with pytest.raises(URLError):
        freshness.main()
    assert not setup_check[1].exists()


def test_invalid_json_remains_an_error(setup_check, monkeypatch):
    monkeypatch.setattr(freshness.urllib.request, "urlopen", lambda *a, **kw: BytesIO(b"not json"))
    with pytest.raises(json.JSONDecodeError):
        freshness.main()
    assert not setup_check[1].exists()
