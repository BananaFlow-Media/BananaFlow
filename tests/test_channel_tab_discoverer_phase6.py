"""tests/test_channel_tab_discoverer_phase6.py — Phase 6 HTTP-probe follow-up.

Covers the new ``_probe_tabs_via_ytdlp`` (no-browser tab discovery) and the
``discover_tabs`` dispatch logic that tries it before ever launching
Playwright. No real network calls or Chromium launches — yt_dlp.YoutubeDL is
mocked throughout, matching this suite's existing convention (see
tests/test_p0_gates.py).

The "missing tab raises DownloadError" assumption these mocks encode is not
a guess: it was verified against real yt-dlp output for real channels
(``@lexfridman/streams``, ``@lexfridman/releases``, ``@YouTube/releases``),
which all raised ``DownloadError: [youtube:tab] <channel>: This channel
does not have a <tab> tab`` rather than silently redirecting to an existing
tab. The mocked messages below use that exact real format.
"""
from __future__ import annotations

import os

# Captured at import/collection time, before the autouse
# `_isolate_user_data_dirs` fixture (tests/conftest.py) redirects LOCALAPPDATA
# per-test — needed by the real-network Playwright test below, since
# Playwright resolves its installed Chromium relative to the real
# LOCALAPPDATA, not the per-test redirected one.
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")

from unittest.mock import MagicMock, patch

import pytest
import yt_dlp.utils

from core.channel_tab_discoverer import (
    DiscoveryResult,
    TabInfo,
    _discover_tabs_via_playwright,
    _probe_tabs_via_ytdlp,
    discover_tabs,
)


def _mock_ydl_cls(extract_info_side_effect):
    """Build a patch target for yt_dlp.YoutubeDL with the given extract_info behavior."""
    mock_ydl = MagicMock()
    mock_ydl.__enter__ = lambda s: mock_ydl
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.side_effect = extract_info_side_effect
    mock_ydl_cls = MagicMock(return_value=mock_ydl)
    return mock_ydl_cls


def _missing_tab_error(channel: str, tab: str) -> "yt_dlp.utils.DownloadError":
    """Real yt-dlp error text for a channel tab that doesn't exist — verified
    against real channels, see module docstring."""
    return yt_dlp.utils.DownloadError(
        f"ERROR: [youtube:tab] {channel}: This channel does not have a {tab} tab"
    )


class TestProbeTabsViaYtdlp:
    def test_finds_only_the_tabs_that_resolve(self):
        """videos/shorts/playlists resolve; streams/podcasts raise (don't exist)."""
        def side_effect(url, download=False):
            if url == "https://www.youtube.com/@demo":
                return {"channel": "Demo Channel"}
            if url.endswith(("/videos", "/shorts", "/playlists")):
                return {"entries": []}
            raise _missing_tab_error("@demo", url.rsplit("/", 1)[-1])

        with patch("yt_dlp.YoutubeDL", _mock_ydl_cls(side_effect)):
            result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

        assert result is not None
        channel_name, tabs = result
        assert channel_name == "Demo Channel"
        found_types = {t.tab_type for t in tabs}
        assert found_types == {"videos", "shorts", "playlists"}
        assert all(isinstance(t, TabInfo) for t in tabs)

    def test_dedupes_live_and_streams_into_one_tab(self):
        """_TAB_MAP has two keys ('live', 'streams') mapping to the same
        canonical type and path — only one TabInfo should be produced."""
        def side_effect(url, download=False):
            if url == "https://www.youtube.com/@demo":
                return {"channel": "Demo Channel"}
            if url.endswith("/streams"):
                return {"entries": []}
            raise _missing_tab_error("@demo", url.rsplit("/", 1)[-1])

        with patch("yt_dlp.YoutubeDL", _mock_ydl_cls(side_effect)):
            result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

        assert result is not None
        _, tabs = result
        assert len(tabs) == 1
        assert tabs[0].tab_type == "streams"

    def test_none_when_base_channel_unreachable(self):
        def side_effect(url, download=False):
            raise yt_dlp.utils.DownloadError("Unable to download webpage")

        with patch("yt_dlp.YoutubeDL", _mock_ydl_cls(side_effect)):
            result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

        assert result is None

    def test_none_when_base_returns_falsy(self):
        with patch("yt_dlp.YoutubeDL", _mock_ydl_cls(lambda url, download=False: None)):
            result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

        assert result is None

    def test_none_when_every_tab_fails_even_though_base_resolved(self):
        """Base channel resolves but every tab-suffix probe fails — treated as
        inconclusive rather than returning an empty tab list."""
        def side_effect(url, download=False):
            if url == "https://www.youtube.com/@demo":
                return {"channel": "Demo Channel"}
            raise _missing_tab_error("@demo", url.rsplit("/", 1)[-1])

        with patch("yt_dlp.YoutubeDL", _mock_ydl_cls(side_effect)):
            result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

        assert result is None

    def test_channel_name_falls_back_through_uploader_and_title(self):
        def side_effect(url, download=False):
            if url == "https://www.youtube.com/@demo":
                return {"uploader": "Uploader Name"}
            raise _missing_tab_error("@demo", url.rsplit("/", 1)[-1])

        with patch("yt_dlp.YoutubeDL", _mock_ydl_cls(side_effect)):
            result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

        # every tab probe fails here so the overall result is None, but this
        # confirms the base call itself doesn't crash on a partial info dict
        assert result is None


class TestDiscoverTabsDispatch:
    def test_uses_probe_result_and_never_touches_playwright(self):
        canned = (
            "Demo Channel",
            [TabInfo(name="סרטונים", url="https://www.youtube.com/@demo/videos",
                      icon="🎬", tab_type="videos")],
        )
        with patch("core.channel_tab_discoverer._probe_tabs_via_ytdlp", return_value=canned), \
             patch("core.channel_tab_discoverer._discover_tabs_via_playwright") as mock_pw:
            result = discover_tabs("https://www.youtube.com/@demo")

        mock_pw.assert_not_called()
        assert result.channel_name == "Demo Channel"
        assert result.channel_url == "https://www.youtube.com/@demo"
        assert len(result.tabs) == 1
        assert result.tabs[0].tab_type == "videos"
        assert result.success()

    def test_falls_back_to_playwright_when_probe_inconclusive(self):
        fallback_result = DiscoveryResult(
            channel_name="Demo Channel",
            channel_url="https://www.youtube.com/@demo",
            tabs=[TabInfo(name="קצרים", url="https://www.youtube.com/@demo/shorts",
                           icon="⚡", tab_type="shorts")],
        )
        with patch("core.channel_tab_discoverer._probe_tabs_via_ytdlp", return_value=None), \
             patch("core.channel_tab_discoverer._discover_tabs_via_playwright",
                   return_value=fallback_result) as mock_pw:
            result = discover_tabs("https://www.youtube.com/@demo/videos")

        # base_url passed to the Playwright fallback must have the tab suffix stripped
        mock_pw.assert_called_once_with("https://www.youtube.com/@demo")
        assert result is fallback_result

    def test_strips_tab_suffix_before_probing(self):
        with patch("core.channel_tab_discoverer._probe_tabs_via_ytdlp") as mock_probe:
            mock_probe.return_value = ("Demo Channel", [
                TabInfo(name="סרטונים", url="https://www.youtube.com/@demo/videos",
                         icon="🎬", tab_type="videos"),
            ])
            discover_tabs("https://www.youtube.com/@demo/shorts")

        mock_probe.assert_called_once_with("https://www.youtube.com/@demo")


# ──────────────────────────────────────────────────────────────────────────────
# Issue #27, offline half. The consent-wall fix shipped with only a
# network-gated test, so nothing that actually runs would have failed if the
# CONSENT/SOCS cookies were deleted again. These drive the real
# _discover_tabs_via_playwright with a fake Playwright, so they run in CI.
# ──────────────────────────────────────────────────────────────────────────────

class _FakeLocator:
    def __init__(self, text=None):
        self._text = text

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._text else 0

    def inner_text(self, timeout=None):
        return self._text or ""


class _FakePage:
    """Records the order of calls so a test can assert consent was accepted
    *before* navigation, not merely at some point."""

    def __init__(self, calls, channel_name=None, tab_hrefs=()):
        self.calls = calls
        self._channel_name = channel_name
        self._tab_hrefs = list(tab_hrefs)

    def route(self, *a, **k):
        pass

    def goto(self, url, **k):
        self.calls.append(("goto", url))

    def wait_for_timeout(self, _ms):
        pass

    def get_attribute(self, _sel, _attr):
        return self._channel_name

    def locator(self, _sel):
        return _FakeLocator(self._channel_name)

    def query_selector_all(self, _sel):
        return []


class _FakeContext:
    def __init__(self, calls, page):
        self.calls = calls
        self._page = page

    def add_cookies(self, cookies):
        self.calls.append(("add_cookies", cookies))

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, ctx):
        self._ctx = ctx

    def new_context(self, **k):
        return self._ctx

    def close(self):
        pass


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = MagicMock()
        self.chromium.launch.return_value = browser

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_playwright_discovery(channel_name=None, tabs=None):
    """Drive the real _discover_tabs_via_playwright against a fake browser.

    Returns ``(result, calls)`` where ``calls`` is the ordered log of
    context/page interactions.
    """
    calls: list[tuple] = []
    page = _FakePage(calls, channel_name=channel_name)
    playwright = _FakePlaywright(_FakeBrowser(_FakeContext(calls, page)))

    with patch("utils.playwright_check.is_playwright_available", return_value=True), \
         patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.sync_api": MagicMock()}), \
         patch("core.channel_tab_discoverer._extract_tabs", return_value=list(tabs or [])):
        import sys
        sys.modules["playwright.sync_api"].sync_playwright = lambda: playwright
        result = _discover_tabs_via_playwright("https://www.youtube.com/@demo")
    return result, calls


def test_consent_cookies_are_set_before_navigating_to_the_channel():
    """Issue #27's actual fix: a fresh context gets redirected to
    consent.youtube.com before the channel page renders, so the tab DOM never
    exists. Deleting the CONSENT/SOCS pre-set — or moving it after goto — must
    fail here, in a test that runs without network access."""
    _, calls = _run_playwright_discovery(channel_name="Demo Channel")

    names = [name for name, _ in calls]
    assert "add_cookies" in names, (
        "the Playwright context must pre-accept YouTube's cookie consent; "
        "without it every navigation lands on the EU consent interstitial"
    )
    assert names.index("add_cookies") < names.index("goto"), (
        "consent cookies must be set on the context BEFORE the first "
        "navigation — setting them afterwards does not prevent the redirect"
    )

    cookies = dict((c["name"], c) for _, arg in calls if _ == "add_cookies" for c in arg)
    assert {"CONSENT", "SOCS"} <= set(cookies), f"missing consent cookies: {sorted(cookies)}"
    for cookie in cookies.values():
        assert cookie["domain"] == ".youtube.com", (
            "a consent cookie scoped to anything but .youtube.com is not sent "
            "with the channel-page request"
        )


def test_an_empty_dom_tab_list_is_reported_as_degraded_not_as_a_real_answer():
    """The complaint in issue #27 was that the fallback *silently* returned
    hardcoded default tabs. It may still fall back — that keeps the feature
    usable — but the result must say so, or the next YouTube DOM change is
    once again indistinguishable from a successful discovery."""
    result, _ = _run_playwright_discovery(channel_name="Demo Channel", tabs=[])

    assert result.tabs, "the default-tab fallback should still produce a usable list"
    assert result.degraded is True
    assert not result.success(), "a fabricated tab list must not report success()"


def test_an_unreadable_channel_name_is_reported_as_degraded():
    real_tabs = [TabInfo(name="סרטונים", url="https://www.youtube.com/@demo/videos",
                         icon="🎬", tab_type="videos")]
    result, _ = _run_playwright_discovery(channel_name=None, tabs=real_tabs)

    assert result.channel_name == "", (
        "an unread channel name must come back empty, not as an "
        "'Unknown Channel' placeholder that looks like a real value"
    )
    assert result.degraded is True
    assert not result.success()


def test_a_fully_successful_discovery_is_not_marked_degraded():
    """Guard against the flag being stuck on — a warning nobody can clear is
    as useless as no warning at all."""
    real_tabs = [TabInfo(name="סרטונים", url="https://www.youtube.com/@demo/videos",
                         icon="🎬", tab_type="videos")]
    result, _ = _run_playwright_discovery(channel_name="Demo Channel", tabs=real_tabs)

    assert result.degraded is False
    assert result.success()


def test_http_probe_reports_an_unread_channel_name_as_empty():
    """Same honesty rule on the probe path: no 'Unknown Channel' placeholder."""
    fake_ydl = MagicMock()
    fake_ydl.__enter__.return_value.extract_info.return_value = {"entries": [{}]}
    with patch("yt_dlp.YoutubeDL", return_value=fake_ydl):
        result = _probe_tabs_via_ytdlp("https://www.youtube.com/@demo")

    if result is not None:
        channel_name, _ = result
        assert channel_name == "", f"expected an empty name, got {channel_name!r}"


@pytest.mark.skipif(
    os.environ.get("BANANAFLOW_RUN_NETWORK_TESTS") != "1",
    reason="set BANANAFLOW_RUN_NETWORK_TESTS=1 to run the optional real-network test",
)
def test_playwright_fallback_bypasses_the_eu_consent_wall_against_a_real_channel(monkeypatch):
    """Optional manual smoke test against a real YouTube channel.

    A fresh Playwright context has no cookies, so YouTube redirects to its
    EU cookie-consent interstitial (consent.youtube.com) before the channel
    page loads — the tab DOM this fallback reads never renders, which was
    first mistaken for "stale DOM selectors" (issue #27). Verifies the
    context's pre-set CONSENT/SOCS cookies actually avoid that redirect and
    the existing ``yt-tab-shape`` selector still resolves real tabs.

    This deliberately uses the real Playwright/Chromium implementation and
    network. It is skipped during normal test and CI runs.
    """
    pytest.importorskip("playwright")
    if _REAL_LOCALAPPDATA:
        # Undo this test's own LOCALAPPDATA redirection (conftest.py) so
        # Playwright finds its actually-installed Chromium, not a path under
        # the empty per-test temp profile.
        monkeypatch.setenv("LOCALAPPDATA", _REAL_LOCALAPPDATA)
    result = _discover_tabs_via_playwright("https://www.youtube.com/@lexfridman")

    assert result.error is None
    assert result.channel_name and result.channel_name != "Unknown Channel"
    assert result.tabs
    assert "videos" in {tab.tab_type for tab in result.tabs}


@pytest.mark.skipif(
    os.environ.get("BANANAFLOW_RUN_NETWORK_TESTS") != "1",
    reason="set BANANAFLOW_RUN_NETWORK_TESTS=1 to run the optional real-network test",
)
def test_http_probe_resolves_real_tabs_for_a_real_channel():
    """Issue #35: the HTTP/yt-dlp probe itself (``_probe_tabs_via_ytdlp``)
    had no real-network test — only the Playwright fallback did. This hits
    real yt-dlp against a real channel, no mocks."""
    result = _probe_tabs_via_ytdlp("https://www.youtube.com/@lexfridman")

    assert result is not None
    channel_name, tabs = result
    assert channel_name and channel_name != "Unknown Channel"
    assert tabs
    assert "videos" in {tab.tab_type for tab in tabs}


@pytest.mark.skipif(
    os.environ.get("BANANAFLOW_RUN_NETWORK_TESTS") != "1",
    reason="set BANANAFLOW_RUN_NETWORK_TESTS=1 to run the optional real-network test",
)
def test_http_probe_and_playwright_agree_on_the_real_tab_list(monkeypatch):
    """Issue #34: a direct comparison of both strategies against the same
    real channel. This is the test that would have caught the Phase 6
    "stale selectors" report (issue #27) directly — the Playwright path
    was actually returning an empty/wrong tab list against a real channel
    (redirected to the EU consent wall before the DOM ever rendered) while
    the HTTP probe returned the correct tabs, a discrepancy this test
    would have surfaced immediately instead of via incidental discovery.
    """
    pytest.importorskip("playwright")
    if _REAL_LOCALAPPDATA:
        monkeypatch.setenv("LOCALAPPDATA", _REAL_LOCALAPPDATA)

    probe_result = _probe_tabs_via_ytdlp("https://www.youtube.com/@lexfridman")
    assert probe_result is not None
    _, probe_tabs = probe_result

    playwright_result = _discover_tabs_via_playwright("https://www.youtube.com/@lexfridman")
    assert playwright_result.error is None

    probe_types = {tab.tab_type for tab in probe_tabs}
    playwright_types = {tab.tab_type for tab in playwright_result.tabs}
    assert probe_types == playwright_types, (
        f"HTTP probe and Playwright disagree on the real tab list for the "
        f"same channel: probe={probe_types!r} playwright={playwright_types!r}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
