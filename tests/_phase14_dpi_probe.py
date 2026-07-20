"""Standalone probe run as a fresh subprocess by test_phase14_dpi_subprocess.py.

Not a pytest module (leading underscore keeps collection away from it): it must
run in its own process per QT_SCALE_FACTOR value, because Qt's High-DPI scale
factor is read once at QApplication construction and a previously created
QApplication in-process would contaminate later factors.

Exits 0 and prints ``PHASE14-DPI-OK`` on success; any failed assertion raises
and exits non-zero with a traceback on stderr.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.i18n import set_language  # noqa: E402
from ui.panels.metadata_editor.panel import MetadataEditorPanel  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])
    panel = MetadataEditorPanel()
    try:
        screen = QApplication.primaryScreen()
        requested_factor = float(os.environ.get("QT_SCALE_FACTOR", "1") or "1")

        # 1) Ordinary widget geometry is authored in logical pixels and must
        #    stay exactly what the source declares -- Qt 6 maps it to the
        #    physical screen on its own; multiplying it again here would
        #    double-scale every control.
        assert panel._apply_btn.minimumWidth() == 92, (
            f"toolbar minimum width drifted from its logical value: "
            f"{panel._apply_btn.minimumWidth()}")
        assert panel._scan_progress.width() == 150
        assert panel._scan_progress.height() == 8
        assert panel._tree_toggle_btn.width() == 28

        # 2) Qt itself must be the one reporting the increased scale, proving
        #    the platform is actually applying QT_SCALE_FACTOR and this is not
        #    a no-op environment.
        if screen is not None:
            reported = screen.devicePixelRatio()
            assert reported >= 1.0
            if requested_factor > 1.0:
                assert reported > 1.0, (
                    f"QT_SCALE_FACTOR={requested_factor} but devicePixelRatio()="
                    f"{reported}; the platform did not apply the scale factor")

        # 3) Long Hebrew and English labels fit their buttons; toolbar actions
        #    are not clipped.
        for language in ("en", "he"):
            set_language(language)
            for attribute in ("_apply_btn", "_revert_btn", "_review_btn"):
                button = getattr(panel, attribute)
                hint = button.sizeHint()
                assert hint.width() <= button.maximumWidth(), (
                    f"{attribute} clips its label at factor={requested_factor} "
                    f"lang={language}")
        set_language("en")

        # 4) Splitter restore fits as one allocation; no pane silently
        #    collapses at any scale, and the whole allocation never exceeds
        #    the available screen width by more than the table's own floor.
        restored = panel._restore_body_sizes()
        assert len(restored) == 3
        available = screen.availableGeometry().width() if screen is not None else sum(restored)
        assert sum(restored) <= max(available, panel._TABLE_OPEN_MIN) + panel._TABLE_OPEN_MIN
        minimums_total = (panel._TREE_RAIL_WIDTH + panel._TABLE_OPEN_MIN
                           + panel._INSPECTOR_RAIL_WIDTH)
        if available >= minimums_total:
            # The table keeps its declared usable minimum whenever
            # mathematically possible -- both side panes can collapse to
            # their rail and still leave the table above its minimum.
            assert restored[1] >= panel._TABLE_OPEN_MIN, (
                f"table pane lost its minimum at factor={requested_factor}: {restored}")
        else:
            # Not even every pane's floor fits the available logical width
            # (a very small screen at a high scale factor): both side panes
            # must still be fully collapsed, and the table gets what is left.
            assert restored[0] == panel._TREE_RAIL_WIDTH
            assert restored[2] == panel._INSPECTOR_RAIL_WIDTH

        # Printed and flushed before any teardown: this process's Qt/offscreen
        # combination on this machine has a known, pre-existing, non-deterministic
        # native exit during interpreter shutdown (unrelated to DPI, documented in
        # the project's accessibility/DPI hardening records).
        # The marker is what the test actually checks for.
        print("PHASE14-DPI-OK", requested_factor, screen.devicePixelRatio() if screen else -1)
        sys.stdout.flush()
        return 0
    finally:
        panel.close()
        panel.deleteLater()
        QApplication.processEvents()


if __name__ == "__main__":
    sys.exit(main())
