"""
ui/dialogs/update_prompt_dialog.py  –  Update notification dialog
==================================================================
One dialog for every "something newer exists" moment — a new BananaFlow
release, outdated runtime components (yt-dlp / yt-dlp-ejs), or both.
Shown by AppWindow after the silent startup check and by the Settings
panel's two manual check buttons.

Updating BananaFlow itself is the primary update path — releases ship the
tested downloader components (yt-dlp, yt-dlp-ejs, and any bundled PO
Token Provider / JS runtime / FFmpeg), so a normal user never has to
know what those are. The prompt therefore shows exactly ONE main
recommendation:

  * App release available (with or without outdated components) →
      "Open Download Page" is the only update action; outdated
      components are mentioned as included in the app update, never as
      a second competing button. Nothing is downloaded or installed
      silently — the browser opens the official release page.
      (A future in-app updater — download, verify, launch installer —
      is documented in README.md; this dialog stays the approval gate.)
  * Components only, source/venv mode → the developer workflow:
      "Update Components" runs the pip upgrade on a background thread
      after this explicit click, then reports the result and reminds
      that a restart is needed.
  * Components only, packaged EXE mode → in-place upgrade is impossible
      (dependencies are baked into the EXE), so the button opens the
      app download page and the body text explains that a BananaFlow update
      is how components get refreshed (and that one may not exist yet).

Secondary choices are always present:

  * Remind me later — with a choice of when (next launch / 1 day /
      3 days / 7 days), recorded per exact version in UpdateStateStore.
  * Skip this version — never notify about these exact versions again;
      a strictly newer future version notifies normally.

Closing the dialog with Esc / the title-bar X is deliberately treated as
"remind me on next launch" (no state change): permanently silencing an
update must be an explicit click on "Skip this version".

``build_update_prompt_text`` and ``collect_update_ids`` are pure
functions (no widgets) so the dialog's content and state bookkeeping are
unit-testable without a QApplication.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QLabel, QMenu, QScrollArea

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

# Fallback target for "get the full app update" when the check that
# found outdated components didn't also carry a ReleaseInfo (frozen
# builds surface component updates as "your app build is stale").
APP_RELEASES_URL = "https://github.com/BananaFlow-Media/BananaFlow/releases"

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
    """Render the dialog body as translated plain text.

    Updating BananaFlow is the primary path: when an app release is
    available, it is the *only* recommendation — outdated components are
    folded into it as "this update includes updated downloader
    components" rather than presented as a second, competing action.
    Component details/pip guidance appear only when there is no app
    release to point at.
    """
    lines: list[str] = []

    if app_release is not None:
        lines.append(t("update_prompt_app_line").format(
            new=app_release.display_version(), cur=CURRENT_VERSION,
        ))
        notes = app_release.short_notes(max_chars=300)
        if notes:
            lines.append(notes)
        if component_updates:
            names = ", ".join(s.display_name for s in component_updates)
            lines.append("")
            lines.append(t("update_prompt_app_includes_components").format(names=names))
        lines.append("")
        lines.append(t("update_prompt_app_note"))
        return "\n".join(lines)

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
    """
    Notification + decision dialog for available updates.

    Parameters
    ----------
    store             : UpdateStateStore that records remind/skip choices.
    app_release       : Newer BananaFlow release, or None.
    component_updates : Outdated components (may be empty).
    parent            : Qt parent widget.
    """

    def __init__(
        self,
        store: UpdateStateStore,
        app_release: Optional[ReleaseInfo] = None,
        component_updates: Optional[list[ComponentStatus]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent, minimum_size=(440, 300), resize_to=(560, 420))
        self._store = store
        self._app_release = app_release
        self._component_updates = list(component_updates or [])
        self._update_ids = collect_update_ids(app_release, self._component_updates)
        self._frozen = not can_update_in_place()
        self._installing = False
        self._install_worker = None

        self.setWindowTitle(t("update_prompt_title"))
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────────

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
        self._body_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse,
        )
        scroll.setWidget(self._body_lbl)
        root.addWidget(scroll, 1)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setVisible(False)
        root.addWidget(self._status_lbl)

        # ── Footer actions ─────────────────────────────────────────────────────
        self._skip_btn = make_button(t("update_skip_btn"), "secondary")
        self._skip_btn.clicked.connect(self._on_skip)

        self._remind_btn = make_button(t("update_remind_btn") + "  ▾", "secondary")
        self._remind_btn.clicked.connect(self._show_remind_menu)

        self._action_buttons = [self._skip_btn, self._remind_btn]
        footer_buttons = [self._skip_btn, self._remind_btn]

        # One clear main recommendation, never two competing update
        # buttons: an available app release IS the update path (it ships
        # the tested components), so the pip button appears only for the
        # component-only, source/venv case — the developer workflow.
        if self._app_release is not None:
            get_app_btn = make_button(t("update_get_app_btn"), "primary")
            get_app_btn.clicked.connect(self._on_get_app_update)
            self._action_buttons.append(get_app_btn)
            footer_buttons.append(get_app_btn)
        elif self._component_updates and not self._frozen:
            self._install_components_btn = make_button(
                t("update_components_btn"), "primary",
            )
            self._install_components_btn.clicked.connect(self._on_install_components)
            self._action_buttons.append(self._install_components_btn)
            footer_buttons.append(self._install_components_btn)
        elif self._component_updates and self._frozen:
            releases_btn = make_button(t("update_open_releases_btn"), "primary")
            releases_btn.clicked.connect(self._on_open_releases_page)
            self._action_buttons.append(releases_btn)
            footer_buttons.append(releases_btn)

        root.addWidget(make_footer(*footer_buttons))

    # ── Decision handlers ──────────────────────────────────────────────────────

    def _on_skip(self) -> None:
        for update_id in self._update_ids:
            self._store.dismiss(update_id)
        self.accept()

    def _show_remind_menu(self) -> None:
        menu = QMenu(self)
        for key, days in REMIND_CHOICES:
            action = menu.addAction(t(key))
            action.triggered.connect(
                lambda _checked=False, d=days: self._on_remind(d)
            )
        menu.exec(self._remind_btn.mapToGlobal(self._remind_btn.rect().bottomLeft()))

    def _on_remind(self, days: Optional[float]) -> None:
        if days is not None:
            for update_id in self._update_ids:
                self._store.snooze(update_id, days)
        # days is None → next launch: no state to record.
        self.accept()

    def _on_get_app_update(self) -> None:
        if self._app_release is not None and self._app_release.release_url:
            QDesktopServices.openUrl(QUrl(self._app_release.release_url))
        self.accept()

    def _on_open_releases_page(self) -> None:
        QDesktopServices.openUrl(QUrl(APP_RELEASES_URL))
        self.accept()

    # ── Component install flow (source/venv mode only) ─────────────────────────

    def _on_install_components(self) -> None:
        from ui.workers.component_install_worker import ComponentInstallWorker

        self._installing = True
        for btn in self._action_buttons:
            btn.setEnabled(False)
        self._status_lbl.setText(t("component_install_running"))
        self._status_lbl.setVisible(True)

        self._install_worker = ComponentInstallWorker(parent=self)
        self._install_worker.completed.connect(self._on_install_completed)
        self._install_worker.start()

    def _on_install_completed(self, success: bool, output_tail: str) -> None:
        self._installing = False
        if success:
            self._status_lbl.setText(t("component_install_ok_msg"))
            # The environment now matches PyPI — nothing left to decide.
            for btn in self._action_buttons:
                btn.setVisible(False)
            close_btn = make_button(t("meta_ok"), "primary")
            close_btn.clicked.connect(self.accept)
            self.layout().addWidget(make_footer(close_btn))
        else:
            self._status_lbl.setText(
                t("component_install_failed_msg").format(detail=output_tail[-400:])
            )
            for btn in self._action_buttons:
                btn.setEnabled(True)
        self._status_lbl.setVisible(True)

    # ── Close semantics ────────────────────────────────────────────────────────

    def reject(self) -> None:  # noqa: N802 - Qt override
        # Esc / title-bar X while pip runs would orphan the worker thread;
        # otherwise closing means "remind me on next launch" (no record).
        if self._installing:
            return
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._installing:
            event.ignore()
            return
        super().closeEvent(event)
