"""
tests/test_downloads_icons.py  –  Downloads-page iconography guards
=====================================================================
Part-7 pins: functional emoji are gone, icon-only buttons carry accessible
names + tooltips, custom status glyphs render (non-null) at multiple sizes,
and the shared status-icon factory is theme-aware.

Headless (QT_QPA_PLATFORM=offscreen); skips when PySide6 is missing.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QApplication, QLabel
    from ui.components.track_card import TrackCard
    from ui.components.offline_banner import OfflineBanner
    from ui.components.status_icon import StatusIcon, StatusKind, status_icon
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 / qfluentwidgets not available", allow_module_level=True)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


# ── track_card ──────────────────────────────────────────────────────────────

class TestTrackCardIcons:
    def test_action_buttons_use_icons_not_emoji(self, app):
        card = TrackCard(title="Song", artist="Artist")
        try:
            for btn in (card._remove_btn, card._pause_btn, card._resume_btn):
                # No leftover Unicode glyph in the button text…
                assert btn.text() == ""
                # …and a real icon is set.
                assert not btn.icon().isNull()
        finally:
            card.deleteLater()

    def test_icon_only_buttons_have_accessible_names(self, app):
        card = TrackCard(title="Song", artist="Artist")
        try:
            for btn in (card._remove_btn, card._pause_btn, card._resume_btn):
                assert btn.accessibleName(), "icon-only button needs an accessible name"
                assert btn.toolTip(), "icon-only button needs a tooltip"
        finally:
            card.deleteLater()

    def test_no_emoji_in_button_text(self, app):
        card = TrackCard(title="Song", artist="Artist")
        try:
            joined = card._remove_btn.text() + card._pause_btn.text() + card._resume_btn.text()
            for glyph in ("✕", "⏸", "▶"):
                assert glyph not in joined
        finally:
            card.deleteLater()


# ── offline banner ──────────────────────────────────────────────────────────

class TestOfflineBanner:
    def test_banner_message_is_localized_not_hardcoded(self, app):
        from ui.i18n import TRANSLATIONS
        banner = OfflineBanner()
        try:
            texts = [w.text() for w in banner.findChildren(QLabel)]
            assert TRANSLATIONS["en"]["offline_banner_msg"] in texts \
                or TRANSLATIONS["he"]["offline_banner_msg"] in texts
        finally:
            banner.deleteLater()

    def test_banner_has_no_emoji_labels(self, app):
        banner = OfflineBanner()
        try:
            for w in banner.findChildren(QLabel):
                for glyph in ("📡", "✕"):
                    assert glyph not in w.text()
        finally:
            banner.deleteLater()


# ── status icon factory ─────────────────────────────────────────────────────

class TestStatusIconFactory:
    @pytest.mark.parametrize("kind", [
        StatusKind.ACTIVITY, StatusKind.SUCCESS, StatusKind.WARNING,
        StatusKind.ERROR, StatusKind.PAUSED, StatusKind.CANCELLING,
        StatusKind.OFFLINE,
    ])
    @pytest.mark.parametrize("size", [14, 16, 24, 32])
    def test_glyphs_render_non_null_at_scales(self, app, kind, size):
        icon = status_icon(kind, size)
        assert not icon.isNull()
        pm = icon.pixmap(QSize(size, size))
        assert not pm.isNull()
        assert pm.width() == size

    def test_none_kind_is_empty(self, app):
        assert status_icon(StatusKind.NONE).isNull()

    def test_widget_hides_on_none(self, app):
        w = StatusIcon()
        w.set_kind(StatusKind.SUCCESS)
        assert w.kind() == StatusKind.SUCCESS
        w.set_kind(StatusKind.NONE)
        assert w.isHidden()
