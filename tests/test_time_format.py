"""
tests/test_time_format.py  –  Shared duration/timestamp formatting
========================================================================
Issue #43: file-modified/downloaded-at timestamps used to be formatted
by three near-identical inline strftime calls (core/history_db.py,
ui/panels/metadata_editor/panel.py, twice) plus one that actually
disagreed with the rest (ui/dialogs/duplicate_files_dialog.py used
DD/MM/YYYY instead of the ISO-style YYYY-MM-DD everywhere else). This
guards the single shared implementation both took over.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from utils.time_format import display_timestamp, seconds_to_str, timestamp_to_str


class TestTimestampToStr:

    def test_formats_a_datetime_as_iso_style(self):
        assert timestamp_to_str(datetime(2026, 3, 5, 14, 30, 0)) == "2026-03-05 14:30"

    def test_formats_a_unix_timestamp_the_same_way_as_the_equivalent_datetime(self):
        dt = datetime(2026, 1, 2, 9, 5, 0)
        assert timestamp_to_str(dt.timestamp()) == timestamp_to_str(dt) == "2026-01-02 09:05"

    def test_accepts_an_int_timestamp(self):
        dt = datetime(2026, 6, 15, 23, 59, 0)
        assert timestamp_to_str(int(dt.timestamp())) == "2026-06-15 23:59"

    def test_never_produces_day_first_or_month_first_ambiguous_output(self):
        # A date where day/month swapped would silently produce a different,
        # equally "valid"-looking date (03 vs 11) -- the exact case that
        # made duplicate_files_dialog.py's old %d/%m/%Y inconsistent with
        # every other panel's %Y-%m-%d.
        result = timestamp_to_str(datetime(2026, 3, 11, 8, 0, 0))
        assert result.startswith("2026-03-11")


class TestDisplayTimestampFollowsTheUiLanguage:
    """Issue #43: a user-facing date must be written the way the reader's
    language writes dates. Showing an Israeli user 2026-03-05 is consistent,
    but consistently foreign — Hebrew writes day-first."""

    @pytest.fixture(autouse=True)
    def _restore_language(self):
        from ui.i18n import current_language, set_language
        original = current_language()
        yield
        set_language(original)

    def test_hebrew_uses_day_first(self):
        from ui.i18n import set_language
        set_language("he")
        assert display_timestamp(datetime(2026, 3, 5, 14, 30)) == "05/03/2026 14:30"

    def test_english_keeps_iso_ordering(self):
        from ui.i18n import set_language
        set_language("en")
        assert display_timestamp(datetime(2026, 3, 5, 14, 30)) == "2026-03-05 14:30"

    def test_time_stays_24_hour_in_both_languages(self):
        from ui.i18n import set_language
        for lang in ("he", "en"):
            set_language(lang)
            assert display_timestamp(datetime(2026, 3, 5, 23, 5)).endswith("23:05"), lang

    def test_day_and_month_are_not_swapped_in_hebrew(self):
        """The failure this format must not introduce: 05/03 meaning May 3rd."""
        from ui.i18n import set_language
        set_language("he")
        # 11 March: unambiguous because 11 cannot be a month read as a day.
        assert display_timestamp(datetime(2026, 3, 11, 8, 0)).startswith("11/03/2026")

    def test_accepts_a_unix_timestamp_like_the_technical_formatter(self):
        from ui.i18n import set_language
        set_language("he")
        dt = datetime(2026, 6, 15, 23, 59)
        assert display_timestamp(dt.timestamp()) == display_timestamp(dt)


class TestTechnicalFormatterStaysLocaleIndependent:
    """timestamp_to_str backs logs, filenames and exported evidence. It must
    never start following the UI language, or a Hebrew user's log lines and
    filenames stop sorting and stop matching everyone else's."""

    @pytest.fixture(autouse=True)
    def _restore_language(self):
        from ui.i18n import current_language, set_language
        original = current_language()
        yield
        set_language(original)

    def test_iso_regardless_of_language(self):
        from ui.i18n import set_language
        for lang in ("he", "en"):
            set_language(lang)
            assert timestamp_to_str(datetime(2026, 3, 5, 14, 30)) == "2026-03-05 14:30", lang


class TestSecondsToStr:
    """Pre-existing behavior, unchanged by this issue -- kept covered since
    this file previously had no dedicated test module at all."""

    def test_minutes_and_seconds(self):
        assert seconds_to_str(65) == "1:05"

    def test_hours_minutes_and_seconds(self):
        assert seconds_to_str(3661) == "1:01:01"

    def test_none_returns_live_label(self):
        assert seconds_to_str(None, live_label="Live") == "Live"

    def test_negative_returns_live_label(self):
        assert seconds_to_str(-5, live_label="—") == "—"
