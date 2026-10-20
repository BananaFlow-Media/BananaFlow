"""Explicit, independent application/component updates and restart choices.

All network, hashing and preparation work belongs to workers. A successful
update never restarts implicitly: the window owns the safe-close gate, and the
entry point launches the replacement only after accepted shutdown.
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QLabel, QMenu, QScrollArea, QProgressBar

from core.component_updates import ComponentStatus, can_update_in_place
from core.update_checker import CURRENT_VERSION, ReleaseInfo
from core.update_state import UpdateStateStore, app_update_id, component_update_id
from ui.dialogs.styled_dialog import (
    StyledDialog,
    add_header,
    make_button,
    make_footer,
    make_root_layout,
)
from ui.i18n import t
from utils.paths import is_frozen
from utils.website import site_url

logger = logging.getLogger(__name__)


def can_prepare_app_update() -> bool:
    from core.app_updates import app_update_support
    return app_update_support().supported


# "Remind me later" choices: (i18n key, days). None = next launch only
# (no persisted snooze — the startup check simply runs again next time).
REMIND_CHOICES: tuple[tuple[str, Optional[float]], ...] = (
    ("update_remind_next_launch", None),
    ("update_remind_1_day", 1.0),
    ("update_remind_3_days", 3.0),
    ("update_remind_7_days", 7.0),
)


# ──────────────────────────────────────────────────────────────────────────────
# Pure helpers (Qt-free, unit-testable)
# ──────────────────────────────────────────────────────────────────────────────

def collect_update_ids(
    app_release: Optional[ReleaseInfo],
    component_updates: list[ComponentStatus],
) -> list[str]:
    """The exact update-state ids this prompt covers. Remind-later and
    skip-this-version apply to every id shown, individually."""
    ids: list[str] = []
    if app_release is not None:
        ids.append(app_update_id(app_release.version))
    for status in component_updates:
        ids.append(component_update_id(status.key, status.latest_version))
    return ids


def build_update_prompt_text(
    app_release: Optional[ReleaseInfo],
    component_updates: list[ComponentStatus],
    frozen: bool,
) -> str:
    """Render both independently available update paths as translated text."""
    lines: list[str] = []

    if app_release is not None:
        lines.append(t("update_prompt_app_line").format(
            new=app_release.display_version(), cur=CURRENT_VERSION,
        ))
        notes = app_release.short_notes(max_chars=300)
        if notes:
            lines.append(notes)
        lines.append("")
        lines.append(t("update_prompt_app_note"))
        lines.append("")

    if component_updates:
        lines.append(t("update_prompt_components_heading"))
        for status in component_updates:
            lines.append("•  " + t("update_prompt_component_line").format(
                name=status.display_name,
                cur=status.installed_version,
                new=status.latest_version,
            ))
        lines.append("")
        lines.append(t("update_prompt_frozen_note") if frozen
                     else t("update_prompt_component_note"))

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Dialog
# ──────────────────────────────────────────────────────────────────────────────

class UpdatePromptDialog(StyledDialog):
    """User-approved updates. Prepared updates remain available after Later."""

    def __init__(
        self, store: UpdateStateStore,
        app_release: Optional[ReleaseInfo] = None,
        component_updates: Optional[list[ComponentStatus]] = None,
        parent=None, *, prepared_update=None, components_ready: bool = False,
        retry_update: bool = False,
    ) -> None:
        super().__init__(parent, minimum_size=(440, 360), resize_to=(640, 520))
        self._store = store
        self._app_release = app_release
        self._component_updates = list(component_updates or [])
        self._update_ids = collect_update_ids(app_release, self._component_updates)
        self._frozen = is_frozen()
        self._installing = False
        self._install_worker = None
        self._app_worker = None
        self._component_result = None
        self._app_result = None
        self._prepared_update = prepared_update
        self._components_ready = components_ready
        self._retry_update = retry_update
        self._app_supported = False
        if self._frozen and (app_release is not None or prepared_update is not None):
            self._app_supported = can_prepare_app_update()
        self.setWindowTitle(t("update_prompt_title"))
        self._build()
        if self._components_ready:
            self._show_components_ready()
        if self._prepared_update is not None:
            self._show_app_ready(self._prepared_update)
        elif self._app_supported and self._app_release is not None:
            # Persisted downloads can be reused offline. Hashing is deliberately
            # not done in the constructor/on the GUI thread.
            self._start_app_worker(None)

    def _build(self) -> None:
        root = make_root_layout(self)
        add_header(root, t("update_prompt_title"), t("update_prompt_subtitle"))
        from qfluentwidgets import BodyLabel
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._body_lbl = BodyLabel(build_update_prompt_text(
            self._app_release, self._component_updates, self._frozen,
        ))
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._body_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll.setWidget(self._body_lbl)
        root.addWidget(scroll, 1)
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._status_lbl.hide()
        root.addWidget(self._status_lbl)
        self._progress = QProgressBar()
        self._progress.setAccessibleName(t("app_update_progress_label"))
        self._progress.hide()
        root.addWidget(self._progress)

        self._action_buttons = []
        if self._app_release is not None or self._prepared_update is not None:
            app_buttons = []
            website = make_button(t("update_get_app_btn"), "secondary")
            website.clicked.connect(self._on_get_app_update)
            app_buttons.append(website)
            if self._app_supported:
                self._app_action_btn = make_button(t("app_update_download_btn"), "primary")
                self._app_action_btn.clicked.connect(self._on_app_action)
                app_buttons.append(self._app_action_btn)
            else:
                unsupported = QLabel(t("app_update_manual_only"))
                unsupported.setWordWrap(True)
                root.addWidget(unsupported)
            self._action_buttons.extend(app_buttons)
            root.addWidget(make_footer(*app_buttons))

        # Independent component repair stays available even with a newer app.
        if self._component_updates and (self._frozen or can_update_in_place()):
            self._install_components_btn = make_button(t("update_components_btn"), "secondary")
            self._install_components_btn.clicked.connect(self._on_install_components)
            self._action_buttons.append(self._install_components_btn)
            root.addWidget(make_footer(self._install_components_btn))

        self._restart_btn = make_button(t("restart_now_btn"), "primary")
        self._restart_btn.clicked.connect(lambda: self._request_restart(None))
        self._restart_btn.hide()
        self._later_btn = make_button(t("restart_later_btn"), "secondary")
        self._later_btn.clicked.connect(self.accept)
        self._later_btn.hide()
        self._skip_btn = make_button(t("update_skip_btn"), "secondary")
        self._skip_btn.clicked.connect(self._on_skip)
        self._remind_btn = make_button(t("update_remind_btn") + "  ▾", "secondary")
        self._remind_btn.clicked.connect(self._show_remind_menu)
        self._action_buttons.extend([self._skip_btn, self._remind_btn, self._restart_btn, self._later_btn])
        root.addWidget(make_footer(self._skip_btn, self._remind_btn, self._later_btn, self._restart_btn))

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._installing = busy
        for button in self._action_buttons:
            button.setEnabled(not busy)
        if message:
            self._status_lbl.setText(message)
            self._status_lbl.show()

    def _on_skip(self) -> None:
        for update_id in self._update_ids:
            self._store.dismiss(update_id)
        self._defer_affordance()
        self.accept()

    def _show_remind_menu(self) -> None:
        menu = QMenu(self)
        for key, days in REMIND_CHOICES:
            action = menu.addAction(t(key))
            action.triggered.connect(lambda _checked=False, d=days: self._on_remind(d))
        menu.exec(self._remind_btn.mapToGlobal(self._remind_btn.rect().bottomLeft()))

    def _on_remind(self, days: Optional[float]) -> None:
        if days is not None:
            for update_id in self._update_ids:
                self._store.snooze(update_id, days)
        self._defer_affordance()
        self.accept()

    def _defer_affordance(self) -> None:
        callback = getattr(self.parentWidget(), "_on_updates_deferred", None)
        if callable(callback):
            callback(self._update_ids)

    def _on_get_app_update(self) -> None:
        QDesktopServices.openUrl(QUrl(site_url("download")))
        self.accept()

    def _on_open_releases_page(self) -> None:
        self._on_get_app_update()

    def _on_install_components(self) -> None:
        if self._installing:
            return
        from ui.workers.component_install_worker import ComponentInstallWorker
        self._set_busy(True, t("component_install_running"))
        self._component_result = None
        self._install_worker = ComponentInstallWorker(parent=self)
        self._install_worker.completed.connect(self._capture_component_result)
        # completed is emitted from run(), before QThread has really stopped.
        # Keep close/restart disabled until its actual finished signal.
        self._install_worker.finished.connect(self._finish_component_worker)
        self._install_worker.start()

    def _capture_component_result(self, success: bool, detail: str) -> None:
        self._component_result = (success, detail)

    def _finish_component_worker(self) -> None:
        result = self._component_result or (False, "")
        self._on_install_completed(*result)

    def _on_install_completed(self, success: bool, output_tail: str) -> None:
        self._set_busy(False)
        if success:
            self._components_ready = True
            self._show_components_ready()
            callback = getattr(self.parentWidget(), "_on_components_prepared", None)
            if callable(callback):
                callback()
        else:
            self._status_lbl.setText(t("component_install_failed_msg").format(detail=output_tail[-400:]))
        self._status_lbl.show()

    def _ready_choices(self) -> None:
        self._skip_btn.hide()
        self._remind_btn.hide()
        self._later_btn.show()

    def _show_components_ready(self) -> None:
        self._status_lbl.setText(t("component_install_ok_msg"))
        self._status_lbl.show()
        self._ready_choices()
        self._restart_btn.show()
        button = getattr(self, "_install_components_btn", None)
        if button is not None:
            button.hide()

    def _on_app_action(self) -> None:
        if self._installing:
            return
        if self._prepared_update is not None:
            self._request_restart(self._prepared_update)
        elif self._app_release is not None:
            self._start_app_worker(self._app_release)

    def _start_app_worker(self, release) -> None:
        from ui.workers.app_update_worker import AppUpdateWorker
        self._restoring = release is None
        self._app_result = None
        self._set_busy(True, t("app_update_checking_saved") if self._restoring else t("app_update_downloading"))
        self._progress.setRange(0, 0)
        self._progress.show()
        self._app_worker = AppUpdateWorker(release, parent=self)
        self._app_worker.progress.connect(self._on_app_progress)
        self._app_worker.completed.connect(self._capture_app_result)
        self._app_worker.finished.connect(self._finish_app_worker)
        self._app_worker.start()

    def _on_app_progress(self, done: int, total: int) -> None:
        if total > 0:
            self._progress.setRange(0, 100)
            self._progress.setValue(min(100, int(done * 100 / total)))
        self._status_lbl.setText(t("app_update_download_progress").format(
            done=f"{done / 1048576:.1f}", total=f"{total / 1048576:.1f}",
        ))

    def _capture_app_result(self, prepared, error: str) -> None:
        self._app_result = (prepared, error)

    def _finish_app_worker(self) -> None:
        self._set_busy(False)
        self._progress.hide()
        prepared, error = self._app_result or (None, "")
        # Do not offer a previous version's saved update for a newer release.
        if prepared is not None and (self._app_release is None or prepared.version == self._app_release.version):
            self._show_app_ready(prepared)
            if getattr(self._app_worker, "update_status", {}).get("status") == "error":
                self._status_lbl.setText(t("app_update_retry_msg"))
            callback = getattr(self.parentWidget(), "_on_app_update_prepared", None)
            if callable(callback):
                callback(prepared)
        elif error:
            logger.log(logging.WARNING, "Application update preparation failed: %s", error)
            self._status_lbl.setText(t("app_update_failed_msg"))
        elif self._restoring:
            self._status_lbl.hide()
        else:
            self._status_lbl.setText(t("app_update_failed_msg"))

    def _show_app_ready(self, prepared) -> None:
        self._prepared_update = prepared
        message = t("app_update_retry_msg") if self._retry_update else t("app_update_ready_msg").format(version=prepared.version)
        self._status_lbl.setText(message)
        self._status_lbl.show()
        self._ready_choices()
        button = getattr(self, "_app_action_btn", None)
        if button is not None:
            button.setText(t("app_update_install_restart_btn"))

    def _request_restart(self, prepared) -> None:
        if self._installing:
            return
        callback = getattr(self.parentWidget(), "request_restart", None)
        if not callable(callback):
            self._status_lbl.setText(t("restart_unavailable_msg"))
            self._status_lbl.show()
            return
        self.accept()
        # Leave the modal event loop before asking the main window to close.
        QTimer.singleShot(0, lambda: callback(prepared))

    def accept(self) -> None:
        if not self._installing:
            super().accept()

    def reject(self) -> None:
        if not self._installing:
            super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._installing:
            event.ignore()
            return
        super().closeEvent(event)
