from __future__ import annotations

import os

import pytest


def _make_panel(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    try:
        from PySide6.QtWidgets import QApplication
        from ui.panels.metadata_editor import MetadataEditorPanel
    except ImportError:
        pytest.skip("PySide6 / qfluentwidgets not available")

    app = QApplication.instance() or QApplication([])
    panel = MetadataEditorPanel()
    return app, panel


def test_toolbar_primary_action_moves_from_folder_to_apply(tmp_path, monkeypatch):
    from core.metadata_models import AudioTrackItem, OriginalTags, ScanResult
    from ui.i18n import current_language, set_language, t
    from ui.theme_manager import get_colors

    previous_language = current_language()
    panel = None
    try:
        set_language("he")
        _app, panel = _make_panel(tmp_path, monkeypatch)
        accent = get_colors().accent

        assert panel._browse_btn.text() == t("meta_browse_folder").strip()
        assert panel._browse_btn.isEnabled()
        assert accent in panel._browse_btn.styleSheet()
        assert not panel._auto_btn.isEnabled()
        assert not panel._auto_container.isEnabled()
        assert not panel._apply_btn.isEnabled()

        track = AudioTrackItem(
            path=tmp_path / "song.mp3",
            folder=tmp_path,
            ext=".mp3",
            original=OriginalTags(title="Song"),
        )
        panel._root_folder = tmp_path
        panel.on_scan_complete(ScanResult(root=tmp_path, tracks=[track], folder_set={tmp_path}))

        assert panel._browse_btn.text() == t("meta_change_folder").strip()
        assert "rgba" in panel._browse_btn.styleSheet()
        assert panel._auto_btn.isEnabled()
        assert panel._auto_container.isEnabled()
        assert not panel._apply_btn.isEnabled()
        assert not panel._revert_btn.isEnabled()

        apply_events = []
        panel.apply_requested.connect(lambda backup_dir, tracks: apply_events.append((backup_dir, tracks)))
        panel._on_apply()
        assert apply_events == []
        revert_events = []
        panel.revert_requested.connect(lambda tracks: revert_events.append(tracks))
        panel._on_revert()
        assert revert_events == []

        track.proposed.title = "New Song"
        panel.on_auto_rules_applied()

        assert panel._apply_btn.isEnabled()
        assert panel._revert_btn.isEnabled()
        assert accent in panel._apply_btn.styleSheet()
        assert panel._auto_btn.isEnabled()
        assert "rgba" in panel._browse_btn.styleSheet()

        panel._on_apply()
        assert len(apply_events) == 1
        assert apply_events[0][1] == [track]
        panel._on_revert()
        assert revert_events == [[track]]
    finally:
        set_language(previous_language)
        if panel is not None:
            panel.deleteLater()
