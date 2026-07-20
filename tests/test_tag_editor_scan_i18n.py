"""
tests/test_tag_editor_scan_i18n.py  –  Phase 3.1 localization guard
====================================================================
build_track_item() surfaces an "unsupported format" tooltip for
audio-looking files the Tag Editor can't read (mt_col_filename tooltip
in ui/models/metadata_table_model.py). It was hardcoded English
regardless of the active UI language — this pins that it now follows
ui.i18n's current language, in both directions.
"""

from __future__ import annotations

from pathlib import Path

from core.metadata_processor import build_track_item
from core.metadata_models import TrackStatus
from ui.i18n import set_language, t


class TestUnsupportedFormatTooltipIsLocalized:

    def test_english(self, tmp_path):
        set_language("en")
        try:
            f = tmp_path / "clip.wav"
            f.write_bytes(b"")
            item = build_track_item(f)
            assert item.status == TrackStatus.UNSUPPORTED
            assert item.error_msg == t("meta_unsupported_format_tooltip")
            assert item.error_msg == "Unsupported format"
        finally:
            set_language("en")

    def test_hebrew(self, tmp_path):
        set_language("he")
        try:
            f = tmp_path / "clip.wav"
            f.write_bytes(b"")
            item = build_track_item(f)
            assert item.status == TrackStatus.UNSUPPORTED
            assert item.error_msg == t("meta_unsupported_format_tooltip")
            assert item.error_msg == "פורמט לא נתמך"
        finally:
            set_language("en")

    def test_keys_exist_in_both_languages(self):
        from ui.i18n import TRANSLATIONS
        assert "meta_unsupported_format_tooltip" in TRANSLATIONS["en"]
        assert "meta_unsupported_format_tooltip" in TRANSLATIONS["he"]
        assert (
            TRANSLATIONS["en"]["meta_unsupported_format_tooltip"]
            != TRANSLATIONS["he"]["meta_unsupported_format_tooltip"]
        )
