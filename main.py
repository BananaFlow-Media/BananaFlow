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
import time
from contextlib import redirect_stdout
from io import StringIO
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

# A windowed build has no stderr, so Python's default "print the traceback"
# handling of an unhandled exception discards it — including exceptions
# escaping worker threads, which silently strand whatever the user started.
# Route all of them to the log file before anything else can fail.
from utils.crash_reporting import install as _install_crash_reporting
_install_crash_reporting()

logger = logging.getLogger(__name__)


def _load_qfluentwidgets_without_console_ad() -> None:
    """Import the UI library without its unconditional Pro advertisement."""
    with redirect_stdout(StringIO()):
        import qfluentwidgets  # noqa: F401


def _create_startup_splash(app, product_name: str, status: str):
    """Show a lightweight first frame before importing the full UI stack."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
    from PySide6.QtWidgets import QSplashScreen

    pixmap = QPixmap(520, 270)
    painter = QPainter(pixmap)
    gradient = QLinearGradient(0, 0, pixmap.width(), pixmap.height())
    gradient.setColorAt(0.0, QColor("#161923"))
    gradient.setColorAt(1.0, QColor("#24203a"))
    painter.fillRect(pixmap.rect(), gradient)

    title_font = QFont(app.font())
    title_font.setPointSize(30)
    title_font.setBold(True)
    painter.setFont(title_font)
    painter.setPen(QColor("#f6c945"))
    painter.drawText(
        pixmap.rect().adjusted(24, 20, -24, -54),
        Qt.AlignmentFlag.AlignCenter,
        product_name,
    )
    painter.end()

    splash = QSplashScreen(
        pixmap,
        Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint,
    )
    splash.setAccessibleName(product_name)
    splash.setAccessibleDescription(status)
    splash.showMessage(
        status,
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        QColor("#f2f2f2"),
    )
    splash.show()
    app.processEvents()
    return splash


def _set_startup_status(app, splash, status: str) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor

    splash.setAccessibleDescription(status)
    splash.showMessage(
        status,
        Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
        QColor("#f2f2f2"),
    )
    app.processEvents()

def _activate_selected_component_overlay():
    """Select a verified downloader overlay after first-frame feedback."""
    try:
        from core.component_overlay import (
            activate_component_overlay,
            should_activate_component_overlay,
        )
        from utils.paths import is_frozen as _is_frozen
        return (
            activate_component_overlay()
            if should_activate_component_overlay(argv=sys.argv, frozen=_is_frozen())
            else None
        )
    except Exception:
        logger.warning(
            "Component-overlay activation failed (using bundled components)",
            exc_info=True,
        )
        return None


def main() -> int:
    startup_started = time.perf_counter()
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

    # Touch input policy. Qt claims raw touch for its windows, which opts the
    # app out of the pan/flick emulation Windows performs for windows that do
    # not — without this the whole UI stops scrolling under a finger. See
    # ui/touch.py for the full reasoning.
    from ui.touch import configure_application as _configure_touch
    _configure_touch(app)

    # 4. Now that QApplication is alive, safely import backend & UI singletons
    from config import AppConfig
    cfg = AppConfig()
    from ui.i18n import apply_language, t
    apply_language(app, cfg.language)
    splash = _create_startup_splash(
        app, PRODUCT_NAME, t("startup_loading_interface"),
    )
    splash_shown = time.perf_counter()
    logger.info("Startup splash shown after %.2fs", splash_shown - startup_started)

    _set_startup_status(app, splash, t("startup_loading_components"))
    _activate_selected_component_overlay()
    from core.services import ServiceContainer
    _load_qfluentwidgets_without_console_ad()
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

    logger.info("Config loaded from %s", cfg._path)

    # 5. Service container — owns all shared backend singletons
    svc = ServiceContainer.create_default(cfg)

    # 6. Main window — receives services via DI and applies the theme before show.
    try:
        _set_startup_status(app, splash, t("startup_opening_workspace"))
        window = AppWindow(config=cfg, services=svc)
        # Prepare expensive navigation pages while the lightweight splash is
        # already visible. Once the interactive window is handed to the user,
        # switching pages cannot introduce a surprise main-thread stall.
        window.prepare_navigation_pages()
        window.show()
        splash.finish(window)
        window_shown = time.perf_counter()
        logger.info(
            "Main window shown after %.2fs (%.2fs after splash)",
            window_shown - startup_started,
            window_shown - splash_shown,
        )
    except Exception:
        splash.close()
        logger.critical("Failed to create main window", exc_info=True)
        svc.close()
        return 1

    # 8. Preflight — surface missing FFmpeg / unwritable output / dead
    #    network. This runs on a worker thread: the network probe and the
    #    Playwright check (which starts Playwright's Node driver) are slow,
    #    and running them here inline froze the already-visible window
    #    until they finished, because app.exec() had not started yet.
    def _on_preflight(preflight) -> None:
        try:
            for line in preflight.details:
                logger.info("[Preflight] %s", line)
            if preflight.all_ok():
                return
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
                # If the dialog itself can't be shown, the warnings must not
                # be lost — fall back to the (English) log rendering.
                logger.warning(
                    "[Preflight] Could not show MessageBox; warnings:\n%s",
                    preflight.warning_text(),
                )
        except Exception:
            logger.warning("[Preflight] Could not report result", exc_info=True)

    preflight_worker = None
    try:
        from ui.workers.preflight_worker import PreflightWorker
        preflight_worker = PreflightWorker(
            output_dir=cfg.output_dir,
            cookies_file=cfg.cookies_file,
            parent=window,
        )
        preflight_worker.completed.connect(_on_preflight)
        preflight_worker.start()
    except Exception:
        logger.warning("[Preflight] check could not start (non-fatal)", exc_info=True)

    # 9. Event loop
    exit_code = app.exec()

    # 9. Cleanup. Playwright owns an asyncio connection inside the preflight
    #    thread. Never let interpreter teardown destroy that connection while
    #    its cancellation task is still pending: request cooperative stop and
    #    join the short-lived worker before returning from main.
    if preflight_worker is not None and preflight_worker.isRunning():
        logger.info("Waiting for startup preflight to stop cleanly")
        preflight_worker.requestInterruption()
        preflight_worker.wait()

    # AppWindow.closeEvent handles most cleanup; svc.close() is a safety net
    # for abnormal exits.
    svc.close()
    logger.info("Application exiting with code %d", exit_code)
    return exit_code


def _run_internal_smoke_test(argv: list[str]) -> int:
    """Hidden internal packaged-verification mode -- not a user feature.

    ``bananaflow.exe --internal-smoke-test tag-editor`` proves the packaged
    executable can reach the Tag Editor through the real production
    navigation path. See core/internal_smoke_test.py for the full contract.
    """
    # Preserve production's selected downloader path for packaged smoke tests.
    # The component-healthcheck mode is routed separately and must never run
    # normal activation against its private staging directory.
    _activate_selected_component_overlay()
    index = argv.index("--internal-smoke-test")
    target = argv[index + 1] if index + 1 < len(argv) else ""
    if target == "tag-editor":
        from core.internal_smoke_test import run_tag_editor_smoke_test
        return run_tag_editor_smoke_test()
    if target == "release-candidate":
        from core.release_candidate_smoke import run_release_candidate_smoke
        return run_release_candidate_smoke()
    print(f'{{"ok": false, "error": "unknown smoke target {target!r}"}}')
    return 2


def _run_component_healthcheck(argv: list[str]) -> int:
    """Hidden isolated validation target for a prepared component overlay."""
    index = argv.index("--component-healthcheck")
    if index + 1 >= len(argv):
        return 2
    from core.component_overlay import run_component_healthcheck
    return run_component_healthcheck(Path(argv[index + 1]))


if __name__ == "__main__":
    # Must be the very first thing in a frozen build. If any code (ours or a
    # dependency) ever starts a child via multiprocessing, PyInstaller's
    # bootloader would otherwise re-run this script from the top in the
    # child — launching a second full copy of the GUI, window and all.
    # freeze_support() makes the child return immediately instead. It is a
    # no-op from source, so it is safe unconditionally.
    import multiprocessing
    multiprocessing.freeze_support()

    if "--component-healthcheck" in sys.argv:
        sys.exit(_run_component_healthcheck(sys.argv))
    if "--internal-smoke-test" in sys.argv:
        sys.exit(_run_internal_smoke_test(sys.argv))
    sys.exit(main())
