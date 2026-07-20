from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX = REPO_ROOT / "docs/metadata_explorer_verification_matrix.md"


def test_metadata_explorer_verification_matrix_covers_required_states():
    text = MATRIX.read_text(encoding="utf-8").casefold()
    required_terms = {
        "empty",
        "1 row",
        "1,000+ rows",
        "ctrl+click",
        "shift+click",
        "ctrl+a",
        "escape",
        "inactive selection",
        "checkbox hover",
        "f2",
        "delete",
        "shift+delete",
        "double-click best fit",
        "context menu",
        "supported audio",
        "unsupported",
        "drag/drop",
        "accessibility",
        "rtl/ltr",
        "dark",
        "light",
        "dpi",
        "high contrast",
    }

    missing = sorted(term for term in required_terms if term not in text)
    assert not missing


def test_metadata_explorer_verification_matrix_keeps_gui_safety_workflow():
    text = MATRIX.read_text(encoding="utf-8")

    assert "list_windows" in text
    assert "activate only" in text
    assert "screenshot/OCR" in text
    assert "If the app window is not clearly identified, stop." in text
