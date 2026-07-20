"""
main.py  –  BananaFlow  entry point
==========================================
Bootstraps the Qt application, loads persistent config, creates the
service container, applies the theme, constructs the main window, and
hands control to the Qt event loop.

Run with:
    python main.py
    python main.py --debug      # verbose console logging
or, after packaging:
    bananaflow
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

# On Windows, the Playwright browser is bundled inside the EXE folder.
# On macOS, Chromium is bundled as loose files (chrome-mac directory) inside
# the .app to avoid nested .app re-signing issues. Point Playwright there.
if getattr(sys, 'frozen', False):
    if sys.platform == 'win32':
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path(sys._MEIPASS) / 'ms-playwright')
    elif sys.platform == 'darwin':
        # Chromium lives in Contents/Resources/ms-playwright (not Contents/MacOS/)
        # so codesign does not scan it when sealing our main executables.
        _resources = Path(sys._MEIPASS).parent / 'Resources'
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(_resources / 'ms-playwright')

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
import logging

# ── Logging MUST be initialised before any other project import ───────────
from utils.logging_config import setup_logging

_debug_mode = "--debug" in sys.argv
setup_logging(debug=_debug_mode)

logger = logging.getLogger(__name__)


def main() -> int:
    logger.info("Starting BananaFlow (debug=%s)", _debug_mode)

    # 1. High-DPI policy must be set before QApplication is constructed
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # 2. Configure policies
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # 3. Construct the global QApplication object IMMEDIATELY
    from version import FULL_VERSION as APP_VERSION, PRODUCT_NAME, COMPANY_NAME
    app = QApplication(sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationDisplayName(PRODUCT_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(COMPANY_NAME)

    # 4. Now that QApplication is alive, safely import backend & UI singletons
    from config import AppConfig
    from core.services import ServiceContainer
    from ui.app_window import AppWindow

    # Activate any downloader components bundled with this build (PO Token
    # Provider plugin + JS runtime) BEFORE the first yt-dlp use, so a
    # packaged EXE is reliable out of the box. Best-effort — never fatal.
    try:
        from core.runtime_components import activate_bundled_components
        _bundled = activate_bundled_components()
        logger.info(
            "Bundled components: provider=%s js_runtime=%s",
            _bundled.provider_modules or "none",
            _bundled.js_runtime_name or "none",
        )
    except Exception:
        logger.warning("Bundled-component activation failed (non-fatal)", exc_info=True)

    cfg = AppConfig()
    logger.info("Config loaded from %s", cfg._path)

    # 5. Service container — owns all shared backend singletons
    svc = ServiceContainer.create_default(cfg)

    # Set UI language and layout direction (single entry point).
    from ui.i18n import apply_language
    apply_language(app, cfg.language)

    # 6. Main window — receives services via DI and applies the theme before show.
    try:
        window = AppWindow(config=cfg, services=svc)
        window.show()
        logger.info("Main window shown")
    except Exception:
        logger.critical("Failed to create main window", exc_info=True)
        svc.close()
        return 1

    # 8. Preflight — surface missing FFmpeg / unwritable output / dead
    #    network up front. Playwright + cookie file diagnostics are
    #    informational and don't block startup. Wrapped in try/except so
    #    a buggy preflight can never crash the app.
    try:
        from error_handler import run_preflight
        preflight = run_preflight(
            output_dir=cfg.output_dir,
            cookies_file=cfg.cookies_file,
        )
        for line in preflight.details:
            logger.info("[Preflight] %s", line)
        if not preflight.all_ok():
            try:
                from qfluentwidgets import MessageBox
                from ui.i18n import render_preflight_warnings, t
                box = MessageBox(
                    t("preflight_warning_title"),
                    render_preflight_warnings(preflight.warnings),
                    window,
                )
                box.yesButton.setText(t("meta_ok"))
                box.cancelButton.hide()
                box.exec()
            except Exception:
                logger.warning(
                    "[Preflight] Could not show MessageBox; warnings:\n%s",
                    preflight.warning_text(),
                )
    except Exception:
        logger.warning("[Preflight] check failed (non-fatal)", exc_info=True)

    # 9. Event loop
    exit_code = app.exec()

    # 9. Cleanup (AppWindow.closeEvent handles most of this,
    #    but svc.close() is a safety net for abnormal exits)
    svc.close()
    logger.info("Application exiting with code %d", exit_code)
    return exit_code


def _run_internal_smoke_test(argv: list[str]) -> int:
    """Hidden internal packaged-verification mode -- not a user feature.

    ``bananaflow.exe --internal-smoke-test tag-editor`` proves the packaged
    executable can reach the Tag Editor through the real production
    navigation path. See core/internal_smoke_test.py for the full contract.
    """
    index = argv.index("--internal-smoke-test")
    target = argv[index + 1] if index + 1 < len(argv) else ""
    if target != "tag-editor":
        print(f'{{"ok": false, "error": "unknown smoke target {target!r}"}}')
        return 2
    from core.internal_smoke_test import run_tag_editor_smoke_test
    return run_tag_editor_smoke_test()


if __name__ == "__main__":
    if "--internal-smoke-test" in sys.argv:
        sys.exit(_run_internal_smoke_test(sys.argv))
    sys.exit(main())
