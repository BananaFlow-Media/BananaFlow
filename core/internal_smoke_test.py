"""Hidden internal packaged-smoke entry point.

Invoked as::

    bananaflow.exe --internal-smoke-test tag-editor

Not a user-facing feature -- it exists to prove the *packaged* executable can
actually reach the Tag Editor through the real production navigation path
(``main.py`` -> ``QApplication`` -> ``ServiceContainer`` -> ``AppWindow`` ->
its Tag Editor sub-interface), not merely that the process starts. The
existing packaged check proved the archive contains the right modules and the
process survives offscreen; it never constructed a single Tag Editor widget.

Contract:

* uses the real production entry point and the real ``AppWindow`` navigation
  -- no shadow/test-only widgets;
* makes no network request (``check_updates`` is forced off before the
  window is built, and the event loop is pumped for less than the 300 ms
  delay before ``AppWindow`` would otherwise start its background workers);
* never modifies media files;
* never writes into the installation directory -- all user data is wherever
  ``AppConfig``/``utils.paths.get_app_data_dir`` resolves to, which the
  caller controls by setting ``APPDATA`` (Windows) before launch. This is
  now true of the Tag Editor draft store too: it used to read the caller's
  home directory instead, so a developer's stray draft could reach in and
  hang the smoke on a modal prompt (F-13);
* runs non-interactively -- ``core.runtime_mode.set_internal_smoke(True)``
  tells the production startup path that no human can answer the draft
  recovery dialog. The check still runs; only the modal prompt is skipped,
  and only inside this process;
* exits 0 with a JSON summary on stdout on success, exits 1 with a JSON
  summary describing the failure otherwise.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback

logger = logging.getLogger(__name__)

#: A minimal, valid 1x1 PNG (transparent), used to prove the packaged image
#: plugins can still decode artwork without needing a real media file.
_TEST_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YA"
    "AAAASUVORK5CYII="
)


def _step(steps: list, name: str, ok: bool, detail: str = "") -> None:
    steps.append({"step": name, "ok": bool(ok), "detail": detail})


def run_tag_editor_smoke_test() -> int:
    """Run the smoke test and print a JSON summary. Returns the process exit code."""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    steps: list = []
    result = {"target": "tag-editor", "ok": False, "steps": steps}

    # Declare the mode before anything constructs a window, so the production
    # startup path never reaches a modal prompt nobody can answer.
    from core.runtime_mode import set_internal_smoke
    set_internal_smoke(True)

    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication.instance() or QApplication(sys.argv[:1])
        _step(steps, "qapplication", True)

        from config import AppConfig
        from core.services import ServiceContainer

        cfg = AppConfig()
        # Never let the smoke test reach the network: the app's own
        # background-worker timer (300 ms after AppWindow is built) starts an
        # update check by default.
        cfg.check_updates = False
        cfg.clipboard_monitor = False
        _step(steps, "config", True, str(cfg._path))

        svc = ServiceContainer.create_default(cfg)
        _step(steps, "service_container", True)

        from ui.i18n import apply_language
        apply_language(app, cfg.language)

        from ui.app_window import AppWindow
        window = AppWindow(config=cfg, services=svc)
        _step(steps, "app_window_constructed", True)

        from ui.panels.metadata_editor.panel import MetadataEditorPanel
        panel = getattr(window, "_metadata_panel", None)
        _step(steps, "tag_editor_panel_constructed",
              isinstance(panel, MetadataEditorPanel),
              type(panel).__name__ if panel is not None else "missing")

        window.show()
        window.switchTo(panel)
        _step(steps, "tag_editor_navigation",
              window.stackedWidget.currentWidget() is panel)

        # The draft store must resolve inside the app-data dir the caller gave
        # us, not the launching user's home. Before F-13 this was false, and a
        # draft left in the developer's home directory hung the packaged smoke
        # on a modal recovery prompt for as long as it was allowed to run.
        from utils.paths import get_app_data_dir
        draft_path = window._metadata_ctrl.draft_path()
        try:
            draft_path.relative_to(get_app_data_dir())
            isolated = True
        except ValueError:
            isolated = False
        _step(steps, "draft_store_uses_app_data_dir", isolated, str(draft_path))

        # Pump the event loop briefly to surface delayed import/resource
        # failures -- deliberately short of the 300 ms mark where AppWindow
        # would otherwise start its (network-touching) background workers.
        import time
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        _step(steps, "event_loop_pumped", True)

        # Hebrew/English direction switch through the production coordinator.
        apply_language(app, "he")
        he_ok = app.layoutDirection() == Qt.LayoutDirection.RightToLeft
        apply_language(app, "en")
        en_ok = app.layoutDirection() == Qt.LayoutDirection.LeftToRight
        _step(steps, "language_direction_switch", he_ok and en_ok,
              f"he_rtl={he_ok} en_ltr={en_ok}")

        # Light/dark theme switch through the production ThemeManager.
        window._theme.apply("dark")
        window._theme.apply("light")
        _step(steps, "theme_switch", window._theme.current == "light")

        # A representative Phase 13/14 dialog, built with safe synthetic
        # in-memory data -- no real file, no network.
        from pathlib import Path

        from core.change_sets import FileIdentity
        from core.file_refresh_service import FieldDifference, StaleFileConflict
        from core.filesystem_monitoring import ExternalChangeState
        from ui.dialogs.external_change_dialog import ExternalChangeReviewDialog

        synthetic_path = Path(cfg._path).with_name("smoke-song.mp3")
        differences = (
            FieldDifference("title", "Old", "Disk", "Local", True, True, True),
        )
        conflict = StaleFileConflict(
            "smoke", 1, synthetic_path, synthetic_path, ExternalChangeState.CONFLICT,
            FileIdentity(str(synthetic_path), 1, 2, 1, 1),
            FileIdentity(str(synthetic_path), 3, 4, 1, 2),
            None, differences, False, 1, 1, 1, "external_change", "smoke")
        dialog = ExternalChangeReviewDialog(conflict, panel)
        dialog_ok = dialog is not None
        dialog.close(); dialog.deleteLater()
        _step(steps, "external_change_dialog_constructed", dialog_ok)

        # Artwork/image decode path through the packaged Qt image plugins.
        import base64
        from PySide6.QtGui import QPixmap
        pixmap = QPixmap()
        loaded = pixmap.loadFromData(base64.b64decode(_TEST_PNG_BASE64))
        _step(steps, "artwork_image_decode", loaded and not pixmap.isNull())

        app.processEvents()
        window.close()
        svc.close()
        _step(steps, "shutdown", True)

        result["ok"] = all(step["ok"] for step in steps)
    except Exception as exc:  # noqa: BLE001 - smoke test must report, not raise
        _step(steps, "exception", False, f"{type(exc).__name__}: {exc}")
        result["traceback"] = traceback.format_exc()
        result["ok"] = False

    payload = json.dumps(result, indent=2)

    # bananaflow.exe is built with console=False (packaging/bananaflow.spec) -- a
    # windowed Win32 subsystem process has no console, so sys.stdout is None
    # and a plain print() would raise (or simply vanish if PyInstaller has
    # redirected it to os.devnull). The result file is therefore the primary
    # channel; stdout is best-effort for the unpackaged/console case.
    result_path = os.environ.get("BANANAFLOW_SMOKE_RESULT_FILE")
    if result_path:
        try:
            with open(result_path, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError:
            logger.exception("Could not write smoke-test result file")
    try:
        print(payload)
    except Exception:
        pass
    return 0 if result["ok"] else 1
