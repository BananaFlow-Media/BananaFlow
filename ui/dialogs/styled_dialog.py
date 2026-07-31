"""
Shared adaptive dialog helpers.

These helpers keep app-owned dialogs visually consistent while preserving each
dialog's existing return values and workflow.
"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QRect, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QAbstractButton,
    QBoxLayout,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui.i18n import t


def _is_app_rtl() -> bool:
    app = QApplication.instance()
    if app is not None:
        return app.layoutDirection() == Qt.LayoutDirection.RightToLeft
    return False


def _apply_text_direction(label: QLabel) -> None:
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(0)
    if _is_app_rtl():
        label.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        label.setAlignment(
            Qt.AlignmentFlag.AlignAbsolute
            | Qt.AlignmentFlag.AlignRight
            | Qt.AlignmentFlag.AlignVCenter
        )
    else:
        label.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        label.setAlignment(
            Qt.AlignmentFlag.AlignAbsolute
            | Qt.AlignmentFlag.AlignLeft
            | Qt.AlignmentFlag.AlignVCenter
        )


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def _qss_theme_is_light(qss: str) -> Optional[bool]:
    qss_l = qss.lower()
    if "#fbfaff" in qss_l or "#faf9ff" in qss_l or "#1a1830" in qss_l:
        return True
    if "#101018" in qss_l or "#0d0d12" in qss_l or "#eeeef5" in qss_l:
        return False
    return None


def _full_theme_qss_from_parent(dialog: QDialog) -> Optional[str]:
    parent = dialog.parentWidget()
    candidates: list[QWidget] = []
    if parent is not None:
        candidates.append(parent)
        window = parent.window()
        if window is not None and window is not parent:
            candidates.append(window)

    for candidate in candidates:
        qss = candidate.styleSheet()
        if "QDialog" in qss and _qss_theme_is_light(qss) is not None:
            return qss
    return None


def _parent_looks_light(dialog: QDialog) -> Optional[bool]:
    parent = dialog.parentWidget()
    if parent is None:
        return None

    candidates = [parent]
    window = parent.window()
    if window is not None and window is not parent:
        candidates.append(window)

    for candidate in candidates:
        theme = _qss_theme_is_light(candidate.styleSheet())
        if theme is not None:
            return theme

    color = parent.palette().color(parent.backgroundRole())
    if color.isValid():
        return color.lightness() >= 128
    return None


def _theme_qss_for_dialog(dialog: QDialog) -> Optional[str]:
    parent_qss = _full_theme_qss_from_parent(dialog)
    if parent_qss:
        return parent_qss

    try:
        from ui.theme_manager import (
            ACCENT_COLOR,
            ThemeManager,
            _build_dark_qss,
            _build_light_qss,
        )

        tm = ThemeManager.instance()
        accent = tm.accent if tm is not None else ACCENT_COLOR
        parent_light = _parent_looks_light(dialog)
        if parent_light is not None:
            return _build_light_qss(accent) if parent_light else _build_dark_qss(accent)
        if tm is not None:
            return tm._current_qss()  # noqa: SLF001 - internal UI helper
    except Exception:
        return None
    return None


def _apply_dialog_palette(dialog: QDialog, qss: str) -> None:
    light = _qss_theme_is_light(qss)
    if light is None:
        return

    bg = QColor("#fbfaff" if light else "#101018")
    text = QColor("#1a1830" if light else "#eeeef5")
    button = QColor("#f3f0fb" if light else "#1e1e2a")

    pal = dialog.palette()
    pal.setColor(QPalette.ColorRole.Window, bg)
    pal.setColor(QPalette.ColorRole.Base, bg)
    pal.setColor(QPalette.ColorRole.AlternateBase, bg)
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Button, button)
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    dialog.setPalette(pal)
    dialog.setAutoFillBackground(True)


def _apply_dialog_theme(dialog: QDialog) -> None:
    """Apply the current app theme (light/dark) directly to a top-level dialog.

    Qt only cascades a stylesheet to widgets under the same top-level window,
    and this app's theme QSS is applied to the main window rather than the
    QApplication — so every dialog must re-apply it explicitly, or it falls
    back to unstyled (theme-mismatched) rendering. This used to copy
    whatever stylesheet happened to be set on the dialog's parent widget,
    but several panel widgets set narrow, cosmetic-only stylesheets (e.g.
    just border-radius rules) with no theme colors in them — copying those
    produced dialogs with the wrong (or default, near-black) background.
    Asking ThemeManager for the current theme's full QSS is unambiguous.
    """
    qss = _theme_qss_for_dialog(dialog)
    if not qss:
        return
    is_tag_editor = bool(dialog.property("tagEditorDialog"))
    parent = dialog.parentWidget()
    while parent is not None and not is_tag_editor:
        is_tag_editor = parent.objectName() == "metadataEditorPage"
        parent = parent.parentWidget()
    if is_tag_editor:
        dialog.setProperty("tagEditorDialog", True)
        try:
            from ui.panels.metadata_editor.shared import tag_dialog_qss
            qss += tag_dialog_qss()
        except ImportError:
            pass
    dialog.setStyleSheet(qss)
    _apply_dialog_palette(dialog, qss)


class CheckMarkBox(QCheckBox):
    """Checkbox rendered as a Windows-like rounded box with a check mark."""

    _MARK_W = 24
    _BOX = 17
    _GAP = 8

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        gap = self._GAP if text_w else 0
        return QSize(self._MARK_W + gap + text_w + 4, max(24, fm.height() + 8))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.sizeHint()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        from ui.theme_manager import get_colors

        colors = get_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rtl = self.layoutDirection() == Qt.LayoutDirection.RightToLeft
        full = self.rect()
        mark_rect = QRect(
            full.right() - self._MARK_W + 1 if rtl else full.left(),
            full.top(),
            self._MARK_W,
            full.height(),
        )
        if self.text():
            if rtl:
                text_rect = QRect(full.left(), full.top(), max(0, full.width() - self._MARK_W - self._GAP), full.height())
                align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            else:
                text_rect = QRect(full.left() + self._MARK_W + self._GAP, full.top(), max(0, full.width() - self._MARK_W - self._GAP), full.height())
                align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            text_color = QColor(colors.text_primary if self.isEnabled() else colors.text_tertiary)
            painter.setPen(text_color)
            painter.drawText(text_rect, align, self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, text_rect.width()))

        center = mark_rect.center()
        box = QRect(0, 0, self._BOX, self._BOX)
        box.moveCenter(center)

        border = QColor(colors.border)
        fill = QColor(colors.surface)
        if self.underMouse() and self.isEnabled():
            fill = QColor(colors.surface2)
        if not self.isEnabled():
            border = QColor(colors.text_tertiary)
            border.setAlpha(130)

        if self.isChecked():
            accent = QColor(colors.accent if self.isEnabled() else colors.text_tertiary)
            fill = QColor(accent)
            fill.setAlpha(82)
            border = QColor(accent)
            border.setAlpha(170)

        painter.setBrush(fill)
        painter.setPen(QPen(border, 1.4))
        painter.drawRoundedRect(box, 4, 4)

        if self.isChecked():
            mark_color = QColor(colors.accent if self.isEnabled() else colors.text_tertiary)
            mark_color.setAlpha(215)
            pen = QPen(mark_color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(center.x() - 5, center.y(), center.x() - 2, center.y() + 4)
            painter.drawLine(center.x() - 2, center.y() + 4, center.x() + 6, center.y() - 5)

        if self.hasFocus():
            focus = QColor(colors.accent)
            focus.setAlpha(120)
            painter.setPen(QPen(focus, 1))
            y = full.bottom() - 1
            painter.drawLine(full.left(), y, full.right(), y)

        painter.end()


def set_button_role(button: QAbstractButton, role: str, *, default: bool = False) -> QAbstractButton:
    """Mark a button for theme-manager QSS styling."""
    button.setProperty("dialogRole", role)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(34)
    if isinstance(button, QPushButton):
        button.setDefault(default)
        button.setAutoDefault(default)
    if role == "cancel" and not button.objectName():
        button.setObjectName("cancelBtn")
    button.setProperty("flatGeometry", True)
    _repolish(button)
    return button


def make_button(text: str, role: str = "secondary", *, icon: Optional[QIcon] = None) -> QPushButton:
    btn = QPushButton(text)
    if icon is not None:
        btn.setIcon(icon)
    set_button_role(btn, role, default=(role == "primary"))
    return btn


def make_icon_button(icon: QIcon, tooltip: str) -> QPushButton:
    btn = QPushButton()
    btn.setIcon(icon)
    btn.setToolTip(tooltip)
    btn.setFixedSize(30, 30)
    set_button_role(btn, "icon")
    return btn


def center_on_parent(dialog: QDialog) -> None:
    """Center a dialog on its parent and keep it inside the visible screen."""
    app = QApplication.instance()
    if app is None:
        return

    parent = dialog.parentWidget()
    screen = parent.screen() if parent is not None and parent.screen() is not None else app.primaryScreen()
    if screen is None:
        return

    available = screen.availableGeometry()
    max_w = max(360, int(available.width() * 0.92))
    max_h = max(280, int(available.height() * 0.88))
    if dialog.width() > max_w or dialog.height() > max_h:
        dialog.resize(min(dialog.width(), max_w), min(dialog.height(), max_h))

    frame = dialog.frameGeometry()
    anchor = parent.frameGeometry().center() if parent is not None and parent.isVisible() else available.center()
    frame.moveCenter(anchor)

    margin = 12
    x = min(max(frame.left(), available.left() + margin), available.right() - frame.width() - margin)
    y = min(max(frame.top(), available.top() + margin), available.bottom() - frame.height() - margin)
    dialog.move(x, y)


def prepare_dialog(
    dialog: QDialog,
    *,
    minimum_size: Optional[tuple[int, int]] = None,
    resize_to: Optional[tuple[int, int]] = None,
    modal: bool = True,
) -> None:
    dialog.setObjectName(dialog.objectName() or "premiumDialog")
    dialog.setModal(modal)
    dialog.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    _apply_dialog_theme(dialog)
    if minimum_size is not None:
        dialog.setMinimumSize(*minimum_size)
    if resize_to is not None:
        dialog.resize(*resize_to)
    dialog.setLayoutDirection(
        Qt.LayoutDirection.RightToLeft if _is_app_rtl() else Qt.LayoutDirection.LeftToRight
    )
    # Bind the deferred callback to the dialog's QObject lifetime.  Without a
    # context, a dialog deleted before the next event turn leaves this lambda
    # alive and it dereferences an invalid PySide wrapper during teardown.
    QTimer.singleShot(0, dialog, lambda: center_on_parent(dialog))


class StyledDialog(QDialog):
    """QDialog with adaptive styling and parent-aware positioning."""

    def __init__(
        self,
        parent=None,
        *,
        minimum_size: Optional[tuple[int, int]] = None,
        resize_to: Optional[tuple[int, int]] = None,
    ) -> None:
        super().__init__(parent)
        prepare_dialog(self, minimum_size=minimum_size, resize_to=resize_to)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        _apply_dialog_theme(self)
        super().showEvent(event)
        QTimer.singleShot(0, self, lambda: center_on_parent(self))


def make_root_layout(dialog: QDialog, *, margins=(20, 18, 20, 16), spacing: int = 14) -> QVBoxLayout:
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return layout


def add_header(
    layout: QVBoxLayout,
    title: str,
    subtitle: str = "",
    *,
    icon: Optional[QIcon] = None,
) -> QFrame:
    header = QFrame()
    header.setObjectName("dialogHeaderFrame")
    header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    header.setLayoutDirection(
        Qt.LayoutDirection.RightToLeft if _is_app_rtl() else Qt.LayoutDirection.LeftToRight
    )
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)
    row.setDirection(
        QBoxLayout.Direction.RightToLeft if _is_app_rtl() else QBoxLayout.Direction.LeftToRight
    )

    if icon is not None:
        icon_lbl = QLabel()
        icon_lbl.setObjectName("dialogHeaderIcon")
        icon_lbl.setPixmap(icon.pixmap(22, 22))
        icon_lbl.setFixedSize(34, 34)
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(icon_lbl)

    text_col = QVBoxLayout()
    text_col.setContentsMargins(0, 0, 0, 0)
    text_col.setSpacing(3)

    title_lbl = QLabel(title)
    title_lbl.setObjectName("dialogTitle")
    title_lbl.setWordWrap(True)
    _apply_text_direction(title_lbl)
    text_col.addWidget(title_lbl)

    if subtitle:
        sub_lbl = QLabel(subtitle)
        sub_lbl.setObjectName("dialogSubtitle")
        sub_lbl.setWordWrap(True)
        _apply_text_direction(sub_lbl)
        text_col.addWidget(sub_lbl)

    row.addLayout(text_col, 1)
    layout.addWidget(header)
    return header


def make_footer(*buttons: QAbstractButton, leading: Iterable[QWidget] = ()) -> QFrame:
    footer = QFrame()
    footer.setObjectName("dialogFooter")
    # Keep dialog actions on the physical right edge in both LTR and RTL.
    # Qt auto-mirrors a QHBoxLayout based on its *widget's* layoutDirection,
    # inherited here from the RTL dialog — setting the layout's Direction
    # alone does not stop that, so the footer's own direction must be
    # pinned to LeftToRight explicitly (verified: without this, OK/Cancel
    # swap sides under a Hebrew/RTL dialog).
    footer.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
    row = QHBoxLayout(footer)
    row.setContentsMargins(0, 12, 0, 0)
    row.setSpacing(8)
    row.setDirection(QBoxLayout.Direction.LeftToRight)
    for widget in leading:
        row.addWidget(widget)
    row.addStretch()
    for button in buttons:
        row.addWidget(button)
    return footer


def make_section(title: str = "") -> QFrame:
    section = QFrame()
    section.setObjectName("dialogSection")
    layout = QVBoxLayout(section)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("dialogSectionTitle")
        lbl.setWordWrap(True)
        _apply_text_direction(lbl)
        layout.addWidget(lbl)
    return section


def section_layout(section: QFrame) -> QVBoxLayout:
    layout = section.layout()
    if not isinstance(layout, QVBoxLayout):
        raise TypeError("dialog section does not use QVBoxLayout")
    return layout


def make_setting_row(checkbox: QCheckBox, *, side_widget: Optional[QWidget] = None) -> QFrame:
    row_frame = QFrame()
    row_frame.setObjectName("dialogSettingRow")
    row = QHBoxLayout(row_frame)
    row.setContentsMargins(10, 8, 8, 8)
    row.setSpacing(8)
    checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    row.addWidget(checkbox, 1)
    if side_widget is not None:
        row.addWidget(side_widget)
    return row_frame


class StyledMessageDialog(StyledDialog):
    """Message dialog with an optional collapsed "Show details" section.

    ``details`` holds raw technical text (upstream tool output, tracebacks,
    log excerpts). It stays hidden behind a toggle so a non-technical user
    reads only the plain-language title and message, while the full
    technical context remains one click away for support/reporting.
    """

    def __init__(
        self,
        title: str,
        text: str,
        parent=None,
        *,
        kind: str = "info",
        accept_text: Optional[str] = None,
        cancel_text: Optional[str] = None,
        show_cancel: bool = False,
        details: str = "",
    ) -> None:
        super().__init__(parent, minimum_size=(360, 150), resize_to=(440, 190))
        self.setWindowTitle(title)
        root = make_root_layout(self, margins=(20, 18, 20, 16), spacing=12)
        add_header(root, title, text)

        self._details_box: Optional[QPlainTextEdit] = None
        self._details_btn: Optional[QPushButton] = None
        details = (details or "").strip()
        if details:
            self._details_btn = make_button(t("details_show_btn"), "secondary")
            btn_row = QHBoxLayout()
            btn_row.setContentsMargins(0, 0, 0, 0)
            # Qt auto-mirrors a plain QHBoxLayout for a RightToLeft widget,
            # so adding in this fixed order already puts the button on the
            # leading edge (left in LTR, right in RTL) — manually swapping
            # the add-order per direction double-mirrors it back to the
            # physical left in both cases.
            btn_row.addWidget(self._details_btn)
            btn_row.addStretch()
            root.addLayout(btn_row)

            box = QPlainTextEdit(details)
            box.setObjectName("dialogDetailsBox")
            box.setReadOnly(True)
            box.setMaximumHeight(150)
            # Raw tool output is Latin/technical — always render it LTR.
            box.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
            box.setVisible(False)
            self._details_box = box
            root.addWidget(box)
            self._details_btn.clicked.connect(self._toggle_details)

        cancel_btn: Optional[QPushButton] = None
        if show_cancel:
            cancel_btn = make_button(cancel_text or t("cancel_btn"), "cancel")
            cancel_btn.clicked.connect(self.reject)

        role = "danger" if kind == "danger" else "primary"
        ok_btn = make_button(accept_text or t("meta_ok"), role)
        ok_btn.clicked.connect(self.accept)

        if cancel_btn is not None:
            root.addWidget(make_footer(cancel_btn, ok_btn))
        else:
            root.addWidget(make_footer(ok_btn))

    def _toggle_details(self) -> None:
        if self._details_box is None or self._details_btn is None:
            return
        show = not self._details_box.isVisible()
        self._details_box.setVisible(show)
        self._details_btn.setText(
            t("details_hide_btn") if show else t("details_show_btn")
        )
        self.adjustSize()


class StyledTextInputDialog(StyledDialog):
    def __init__(
        self,
        title: str,
        prompt: str,
        parent=None,
        *,
        text: str = "",
    ) -> None:
        super().__init__(parent, minimum_size=(420, 170), resize_to=(500, 210))
        self.setWindowTitle(title)
        self.text_value = text

        root = make_root_layout(self, margins=(20, 18, 20, 16), spacing=12)
        add_header(root, title, prompt)
        self.line_edit = QLineEdit(text)
        self.line_edit.setObjectName("dialogTextInput")
        self.line_edit.selectAll()
        root.addWidget(self.line_edit)

        cancel_btn = make_button(t("cancel_btn"), "cancel")
        ok_btn = make_button(t("meta_ok"), "primary")
        cancel_btn.clicked.connect(self.reject)
        ok_btn.clicked.connect(self._accept)
        root.addWidget(make_footer(cancel_btn, ok_btn))

    def _accept(self) -> None:
        self.text_value = self.line_edit.text()
        self.accept()


def show_info(parent, title: str, text: str, *, button_text: Optional[str] = None,
              details: str = "") -> None:
    StyledMessageDialog(title, text, parent, kind="info", accept_text=button_text,
                        details=details).exec()


def show_warning(parent, title: str, text: str, *, button_text: Optional[str] = None,
                 details: str = "") -> None:
    StyledMessageDialog(title, text, parent, kind="warning", accept_text=button_text,
                        details=details).exec()


def confirm(
    parent,
    title: str,
    text: str,
    *,
    accept_text: Optional[str] = None,
    cancel_text: Optional[str] = None,
    danger: bool = False,
    details: str = "",
) -> bool:
    dlg = StyledMessageDialog(
        title,
        text,
        parent,
        kind="danger" if danger else "question",
        accept_text=accept_text,
        cancel_text=cancel_text,
        show_cancel=True,
        details=details,
    )
    return dlg.exec() == QDialog.DialogCode.Accepted


def get_text(parent, title: str, prompt: str, *, text: str = "") -> tuple[str, bool]:
    dlg = StyledTextInputDialog(title, prompt, parent, text=text)
    ok = dlg.exec() == QDialog.DialogCode.Accepted
    return dlg.text_value, ok
