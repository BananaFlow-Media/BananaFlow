"""
ui/dialogs/youtube_doctor_dialog.py  –  YouTube Doctor results dialog
========================================================================
Shows the structured, fully-offline core.youtube_doctor report to the
user. Read-only / informational — a single "OK" button, no destructive
actions. Never displays cookie values: the report itself never carries
any (see core/youtube_doctor.py's CookieDiagnostics).

``build_report_text()`` is a pure, Qt-free function so the dialog's
content can be unit-tested without a QApplication.
"""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QScrollArea

from core.youtube_doctor import DoctorStatus, YoutubeDoctorReport
from ui.dialogs.styled_dialog import (
    StyledDialog,
    add_header,
    make_button,
    make_footer,
    make_root_layout,
)
from ui.i18n import t

_STATUS_ICON = {
    DoctorStatus.PASS: "✅",
    DoctorStatus.WARN: "⚠",
    DoctorStatus.FAIL: "❌",
}

# core.youtube_doctor DoctorCheck.category -> i18n key for its display label.
_CATEGORY_LABEL_KEYS = {
    "yt_dlp_version": "youtube_doctor_cat_yt_dlp_version",
    "yt_dlp_ejs": "youtube_doctor_cat_yt_dlp_ejs",
    "js_runtime": "youtube_doctor_cat_js_runtime",
    "cookies": "youtube_doctor_cat_cookies",
    "po_token_provider": "youtube_doctor_cat_po_token_provider",
    "youtube_reliability_mode": "youtube_doctor_cat_reliability_mode",
}

_YES_MAYBE_NO_KEYS = {
    "yes": "youtube_doctor_yes",
    "maybe": "youtube_doctor_maybe",
    "no": "youtube_doctor_no",
}


def _check_message(check) -> str:
    """Render one check's message in the active UI language.

    Checks built by core.youtube_doctor carry a message_key + params
    (English templates are injected into TRANSLATIONS["en"] from the
    same dict core renders from, so English output is identical);
    checks constructed directly (tests, future ad-hoc) fall back to
    their canonical English ``message``.
    """
    if check.message_key:
        return t(check.message_key, **check.message_params)
    return check.message


def _check_detail(check) -> str:
    """Render one check's recommended action in the active UI language."""
    if check.detail_key:
        return t(check.detail_key, **check.detail_params)
    return check.detail


def build_report_text(report: YoutubeDoctorReport) -> str:
    """Render a YoutubeDoctorReport as translated, human-readable text.
    Pure function — no Qt widgets — so it's directly unit-testable."""
    lines: list[str] = []

    for check in report.checks:
        label = t(_CATEGORY_LABEL_KEYS.get(check.category, check.category))
        icon = _STATUS_ICON[check.status]
        lines.append(f"{icon}  {label} — {_check_message(check)}")

    lines.append("")
    lines.append(
        f"{t('youtube_doctor_ready_label')}: "
        f"{t(_YES_MAYBE_NO_KEYS[report.ready_for_public_downloads])}"
    )
    lines.append(
        f"{t('youtube_doctor_cookies_label')}: "
        f"{t(_YES_MAYBE_NO_KEYS[report.cookies_available_for_gated])}"
    )
    po_key = "yes" if report.po_token_provider_ready else "no"
    lines.append(f"{t('youtube_doctor_po_label')}: {t(_YES_MAYBE_NO_KEYS[po_key])}")

    # Translated equivalent of report.recommended_actions(): every WARN/
    # FAIL check's detail, rendered in the active UI language.
    actions = [
        _check_detail(check)
        for check in report.checks
        if check.status in (DoctorStatus.WARN, DoctorStatus.FAIL) and check.detail
    ]
    if actions:
        lines.append("")
        lines.append(f"{t('youtube_doctor_actions_title')}:")
        lines.extend(f"• {a}" for a in actions)

    return "\n".join(lines)


class YoutubeDoctorDialog(StyledDialog):
    """Read-only dialog showing a YoutubeDoctorReport."""

    def __init__(self, report: YoutubeDoctorReport, parent=None) -> None:
        super().__init__(parent, minimum_size=(420, 320), resize_to=(540, 560))
        self.setWindowTitle(t("youtube_doctor_dialog_title"))

        root = make_root_layout(self)
        add_header(root, t("youtube_doctor_dialog_title"), t("youtube_doctor_dialog_subtitle"))

        from qfluentwidgets import BodyLabel

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body_lbl = BodyLabel(build_report_text(report))
        body_lbl.setWordWrap(True)
        scroll.setWidget(body_lbl)
        root.addWidget(scroll, 1)

        close_btn = make_button(t("meta_ok"), "primary")
        close_btn.clicked.connect(self.accept)
        root.addWidget(make_footer(close_btn))


def show_youtube_doctor_dialog(report: YoutubeDoctorReport, parent=None) -> None:
    YoutubeDoctorDialog(report, parent).exec()
