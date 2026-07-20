from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_metadata_center_widgets_have_accessible_names():
    source = (REPO_ROOT / "ui/panels/metadata_editor/panel.py").read_text(encoding="utf-8")

    for key in (
        "meta_a11y_file_tree",
        "meta_a11y_file_tree_desc",
        "meta_a11y_details_table",
        "meta_a11y_details_table_desc",
        "meta_a11y_table_header",
        "meta_a11y_zoom_out",
        "meta_a11y_zoom_value",
        "meta_a11y_zoom_in",
    ):
        assert f't("{key}")' in source
