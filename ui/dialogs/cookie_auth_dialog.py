"""
ui/dialogs/cookie_auth_dialog.py  –  Cookie authentication choice + manual import
===================================================================================
Two small dialogs used by AppWindow._run_cookie_wizard_ui:

  ask_cookie_auth_choice()   – "sign in via app browser" vs "import cookies manually"
  ManualCookieImportDialog   – instructions + link to the cookie-export extension

Zero business logic here — both just resolve a user choice. The caller
(ui/app_window.py) drives core.cookie_wizard.run_cookie_wizard() and the
actual file picker.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from ui.dialogs.styled_dialog import (
    StyledDialog,
    add_header,
    make_button,
    make_footer,
    make_root_layout,
)
from ui.i18n import t

# Official Chrome Web Store listing for "Get cookies.txt LOCALLY" (kairi003) —
# the verified-safe, open-source successor to the original "Get cookies.txt"
# extension (which is now malware). Confirmed against the extension's own
# GitHub README (github.com/kairi003/Get-cookies.txt-LOCALLY), not typed
# from memory — do not change without re-verifying against that source.
COOKIE_EXPORT_EXTENSION_URL = (
    "https://chromewebstore.google.com/detail/"
    "get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc"
)


def ask_cookie_auth_choice(parent) -> Optional[str]:
    """
    Ask the user how to provide YouTube auth cookies.

    Returns "app_browser", "manual", or None if dismissed (X / Esc)
    without picking either option.
    """
    dlg = StyledDialog(parent, minimum_size=(380, 170), resize_to=(460, 210))
    dlg.setWindowTitle(t("cookie_auth_choice_title"))
    root = make_root_layout(dlg)
    add_header(root, t("cookie_auth_choice_title"), t("cookie_auth_choice_body"))

    result: dict[str, Optional[str]] = {"choice": None}

    def _pick(choice: str) -> None:
        result["choice"] = choice
        dlg.accept()

    manual_btn = make_button(t("cookie_auth_choice_manual_btn"), "secondary")
    manual_btn.clicked.connect(lambda: _pick("manual"))

    app_btn = make_button(t("cookie_auth_choice_app_browser_btn"), "primary")
    app_btn.clicked.connect(lambda: _pick("app_browser"))

    root.addWidget(make_footer(manual_btn, app_btn))
    dlg.exec()
    return result["choice"]


class ManualCookieImportDialog(StyledDialog):
    """
    Instructions dialog for the manual-cookie-import fallback.

    "Open extension page" does NOT close the dialog — the user needs to
    switch to their browser, log in, and export cookies before coming
    back. "Choose file" closes with Accepted; the caller then shows its
    own QFileDialog, so this dialog only decides *whether* to show it.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent, minimum_size=(420, 220), resize_to=(480, 260))
        self.setWindowTitle(t("manual_cookie_import_title"))
        root = make_root_layout(self)
        add_header(
            root,
            t("manual_cookie_import_title"),
            t("manual_cookie_import_instructions"),
        )

        open_ext_btn = make_button(t("manual_cookie_import_open_extension_btn"), "secondary")
        open_ext_btn.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(COOKIE_EXPORT_EXTENSION_URL))
        )

        choose_btn = make_button(t("manual_cookie_import_choose_file_btn"), "primary")
        choose_btn.clicked.connect(self.accept)

        root.addWidget(make_footer(open_ext_btn, choose_btn))
