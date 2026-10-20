"""Real GUI lifecycle evidence for explicitly invoked disposable updater smoke.

This is not reached by ordinary startup and never launches an updater itself.
The core smoke records preservation evidence and performs the helper handoff
only after this function proves that the production close path was accepted.
"""
from __future__ import annotations


def run_gui_update_smoke(prepared=None) -> dict:
    """Show the real window/navigation, then exercise its safe close boundary.

    ``prepared`` selects the old-build Install/restart path. With no prepared
    update, the new build opens normally and closes without requesting restart.
    All settings come from the isolated smoke's real configuration unchanged.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from config import AppConfig
    from core.runtime_mode import is_internal_smoke, set_internal_smoke
    from core.services import ServiceContainer
    from ui.app_window import AppWindow
    from ui.i18n import apply_language
    from version import FULL_VERSION, PRODUCT_NAME, COMPANY_NAME

    prior_smoke = is_internal_smoke()
    set_internal_smoke(True)
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(FULL_VERSION)
    app.setOrganizationName(COMPANY_NAME)
    config = AppConfig()
    apply_language(app, config.language)
    services = ServiceContainer.create_default(config)
    window = None
    result = {
        "version": FULL_VERSION,
        "gui_shown": False,
        "navigation_prepared": False,
        "shutdown_accepted": False,
        "safe_restart_requested": False,
        "preferences": {
            "language": config.language,
            "theme": config.theme,
            "output_dir": config.output_dir,
            "check_updates": config.check_updates,
        },
    }
    try:
        window = AppWindow(config=config, services=services)
        window.prepare_navigation_pages()
        result["navigation_prepared"] = (
            window._metadata_panel is not None and window._settings_panel is not None
        )
        window.show()

        def close_after_first_frame() -> None:
            result["gui_shown"] = window.isVisible()
            if prepared is not None:
                result["safe_restart_requested"] = window.request_restart(prepared)
            else:
                # Keep tray_on_close unchanged. Hiding the test's actual tray
                # icon allows the same normal close route to reach cleanup.
                if window._tray is not None:
                    window._tray.hide()
                window.close()

        QTimer.singleShot(150, window, close_after_first_frame)
        app.exec()
        result["shutdown_accepted"] = bool(window._shutdown_accepted)
        if not result["shutdown_accepted"]:
            raise RuntimeError("The GUI did not accept its normal safe shutdown.")
        if prepared is not None and not result["safe_restart_requested"]:
            raise RuntimeError("The GUI did not accept the explicit update restart request.")
        if not result["gui_shown"] or not result["navigation_prepared"]:
            raise RuntimeError("The packaged GUI did not finish navigation startup.")
        result["ok"] = True
        return result
    finally:
        # Do not bypass a close veto or destroy running window-owned workers.
        # Normal success already closed services through AppWindow.closeEvent.
        if window is None or getattr(window, "_shutdown_accepted", False):
            services.close()
        set_internal_smoke(prior_smoke)
