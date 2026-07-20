"""
ui/components/offline_banner.py  –  Network offline notification banner
========================================================================
A slim, dismissible top-of-window banner that appears when the
OfflineMonitor detects network loss and disappears when connectivity
is restored.

Shown/hidden by AppWindow in response to OfflineMonitor signals:
    monitor.went_offline.connect(banner.show)
    monitor.came_online.connect(banner.hide)

The user can also manually dismiss the banner with the × button.

Text is localized (ui.i18n) and the icons come from the shared vector
sets (status_icon / FluentIcon) — no emoji, so the banner stays visually
consistent with the rest of the app and legible at every DPI scale.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget
from qfluentwidgets import FluentIcon, ToolButton

from ui.components.status_icon import StatusKind, status_icon
from ui.i18n import t


class OfflineBanner(QFrame):
    """
    A 40-px high warning strip displayed above the main content area
    when the app is offline.

    Does not emit any signals – it is purely informational.
    """

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._build()
        self.hide()   # hidden by default

    def _build(self) -> None:
        self.setFixedHeight(40)
        self.setObjectName("offlineBanner")
        self.setStyleSheet("""
            #offlineBanner {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #ef4444, stop:1 #dc2626);
                border: none;
                border-radius: 0;
            }
        """)

        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 12, 0)
        row.setSpacing(10)

        # White "offline" vector glyph on the red banner (baked at a fixed
        # colour so it reads against the gradient, unlike the theme-tinted
        # StatusIcon used in the footer).
        icon = QLabel()
        icon.setFixedSize(18, 18)
        icon.setPixmap(status_icon(StatusKind.OFFLINE, 18, "#ffffff").pixmap(QSize(18, 18)))
        icon.setStyleSheet("background: transparent;")
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignVCenter)

        msg = QLabel(t("offline_banner_msg"))
        msg.setStyleSheet(
            "color: #ffffff; font-size: 12px; font-weight: 600; "
            "background: transparent;"
        )
        msg.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(msg)

        close_btn = ToolButton(self)
        close_btn.setIcon(FluentIcon.CLOSE.icon(color=QColor("#ffffff")))
        close_btn.setIconSize(QSize(11, 11))
        close_btn.setFixedSize(26, 26)
        close_btn.setToolTip(t("offline_banner_close"))
        close_btn.setAccessibleName(t("offline_banner_close"))
        close_btn.setStyleSheet("""
            ToolButton {
                background: rgba(255,255,255,0.15);
                border: none;
                border-radius: 4px;
            }
            ToolButton:hover { background: rgba(255,255,255,0.30); }
        """)
        close_btn.clicked.connect(self.hide)
        row.addWidget(close_btn)
