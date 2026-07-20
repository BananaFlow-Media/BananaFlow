"""
ui/panels/options_bar.py  –  Type / format / quality / output controls bar
==============================================================================
A compact horizontal bar shown above the queue panel that lets the user
pick the media type (audio/video), output format, quality, output folder,
and toggle the clipboard monitor — all without opening Settings.

It reads its initial state from AppConfig on construction, writes every
user change straight back to AppConfig (type, format, quality, output
folder, clipboard monitor all survive a restart), and exposes
apply_config() so AppWindow can sync it after a Settings save.

Model
-----
Type    : "audio" | "video"                            (config.media_type)
Format  : "mp3" | "m4a" | "flac" | "opus" — audio only  (config.audio_format)
          video is always "mp4" today, not user-selectable (config.video_format)
Quality : media-type-specific preset ID                 (config.audio_quality / video_quality)

Signals emitted upward
----------------------
options_changed()     Any control changed; AppWindow reads get_options().
browse_requested()    User clicked the folder button (AppWindow opens dialog).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QPoint, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QLabel,
    QSizePolicy, QWidget,
)
from qfluentwidgets import (
    ComboBox, FluentIcon, LineEdit, SwitchButton, ToolButton,
)
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from config import AppConfig
from core.media_formats import AUDIO_FORMATS
from core.quality_presets import (
    VIDEO_QUALITY_PRESETS,
    audio_presets_for_codec,
    coerce_audio_quality_id,
    coerce_video_quality_id,
    default_audio_quality_id_for_codec,
)
from ui.direction import force_ltr, force_ltr_input, force_ltr_label, quality_display
from ui.i18n import current_language, t
from ui.theme_manager import ThemeManager, get_colors


# ──────────────────────────────────────────────────────────────────────────────
# Layout constants
# ──────────────────────────────────────────────────────────────────────────────

# The real combo is shrunk to show only its own dropdown arrow (see
# _make_group). The icon is always drawn at x=(width-22), 10px wide, so the
# *minimum* width that doesn't clip it is 22 — going much above that just
# reintroduces empty space to the icon's own left, since the formula is a
# fixed offset from the combo's own right edge, not from its content.
_ARROW_ONLY_WIDTH = 26


# ──────────────────────────────────────────────────────────────────────────────
# _AnchoredComboBox  –  a ComboBox whose popup opens under a separate widget
# ──────────────────────────────────────────────────────────────────────────────

class _AnchoredComboBox(ComboBox):
    """A ComboBox shrunk to just its arrow glyph (see _ARROW_ONLY_WIDTH),
    whose popup menu must still open flush under the full-size visible
    pill rather than under its own tiny sliver.

    qfluentwidgets' ComboBoxBase._showComboMenu() always anchors the popup
    to the combo's own width()/mapToGlobal() (hardcoded, no override hook),
    so once the combo is shrunk to 26px the popup ends up centred on that
    sliver instead of the pill. _showComboMenu() below is a copy of the
    base implementation with only the geometry calls swapped for an
    `anchor` widget's — everything else (menu construction, item-click
    wiring, WA_DeleteOnClose) is untouched, byte-for-byte identical to
    upstream.

    Crash history (do not re-widen this override without re-reading this):
    an earlier version additionally rewired _onItemClicked/item-click
    connection and had no _onDropMenuClosed override. That combination
    reproducibly segfaulted Qt6Widgets, confirmed via WinDbg/cdb against
    real crash dumps. Root cause: the popup is WA_DeleteOnClose, so
    closing it schedules deleteLater() on a widget (its item-list view)
    that still holds keyboard focus. When that deferred delete is later
    processed, QWidget::~QWidget()'s deleteChildren() calls clearFocus()
    on the still-focused child, which synchronously dispatches a
    focus-change notify() into whatever widget Qt reassigns focus to
    next — doing that mid-teardown reproducibly segfaults inside
    Qt6Widgets (access violation on a stale pointer). This is present in
    the *stock* implementation too, not just the old override (a plain
    ComboBox in this same app crashed too, just far less often). The
    _onDropMenuClosed override moves focus to this combo itself the
    moment the popup starts closing — synchronously, well before the
    deferred delete runs — so there's nothing left for the destructor to
    reassign later. That fix is load-bearing; keep it paired with any
    _showComboMenu override here.
    """

    def __init__(self, anchor: QWidget, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._anchor = anchor

    def _showComboMenu(self) -> None:
        if not self.items:
            return

        menu = self._createComboMenu()
        # The popup is Qt.Popup-flagged, i.e. a real top-level window, not
        # a plain child, so choose its direction explicitly. Hebrew rows use
        # RTL alignment, while the technical values inside each row are kept
        # LTR by Unicode isolation in quality_display().
        popup_direction = (
            Qt.LayoutDirection.RightToLeft
            if current_language() == "he"
            else Qt.LayoutDirection.LeftToRight
        )
        menu.setLayoutDirection(popup_direction)
        menu.view.setLayoutDirection(popup_direction)
        for item in self.items:
            action = QAction(item.icon, item.text)
            action.setEnabled(item.isEnabled)
            menu.addAction(action)

        menu.view.itemClicked.connect(lambda i, v=menu.view: self._onItemClicked(v.row(i)))

        anchor = self._anchor
        if menu.view.width() < anchor.width():
            menu.view.setMinimumWidth(anchor.width())
            menu.adjustSize()

        menu.setMaxVisibleItems(self.maxVisibleItems())
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.closedSignal.connect(self._onDropMenuClosed)
        self.dropMenu = menu

        if self.currentIndex() >= 0 and self.items:
            menu.setDefaultAction(menu.actions()[self.currentIndex()])

        x = -menu.width() // 2 + menu.layout().contentsMargins().left() + anchor.width() // 2
        pd = anchor.mapToGlobal(QPoint(x, anchor.height()))
        hd = menu.view.heightForAnimation(pd, MenuAnimationType.DROP_DOWN)

        pu = anchor.mapToGlobal(QPoint(x, 0))
        hu = menu.view.heightForAnimation(pu, MenuAnimationType.PULL_UP)

        if hd >= hu:
            menu.view.adjustSize(pd, MenuAnimationType.DROP_DOWN)
            menu.exec(pd, aniType=MenuAnimationType.DROP_DOWN)
        else:
            menu.view.adjustSize(pu, MenuAnimationType.PULL_UP)
            menu.exec(pu, aniType=MenuAnimationType.PULL_UP)

    def _onItemClicked(self, index):
        if index < 0 or index >= len(self.items):
            return
        super()._onItemClicked(index)

    def _onDropMenuClosed(self) -> None:
        self.setFocus(Qt.FocusReason.PopupFocusReason)
        super()._onDropMenuClosed()


# ──────────────────────────────────────────────────────────────────────────────
# _PillFrame  –  a QFrame whose whole area opens an attached ComboBox's menu
# ──────────────────────────────────────────────────────────────────────────────

class _PillFrame(QFrame):
    """A plain QFrame pill that forwards a click anywhere on it to a hidden
    ComboBox's dropdown. Needed because the combo itself is shrunk down to
    just its own arrow glyph (see _make_group) — without this, only that
    small arrow sliver would be clickable instead of the whole chip."""

    def __init__(self, target_combo: "ComboBox" = None, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._target = target_combo

    def mouseReleaseEvent(self, event) -> None:
        if self._target.isEnabled():
            self._target._toggleComboMenu()  # noqa: SLF001 - same pattern as the widget's own click handler
        super().mouseReleaseEvent(event)


# ──────────────────────────────────────────────────────────────────────────────
# OptionsBar
# ──────────────────────────────────────────────────────────────────────────────

class OptionsBar(QFrame):
    """
    Compact one-row controls bar.

    Parameters
    ----------
    config : AppConfig – read on construction; written back on apply_config().
    parent : Optional Qt parent.
    """

    options_changed = Signal()

    def __init__(self, config: AppConfig, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._config = config
        self._syncing_config = False
        self._build()
        self.apply_config(config)

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_options(self) -> dict:
        """
        Return a dict of the current control values.

        Keys
        ----
        media_type    : "audio" | "video"
        quality_label : stable preset ID, e.g. "audio_mp3_320" or
                        "video_1080". NOT the translated on-screen text.
        audio_format  : "mp3" | "m4a" | "flac" | "opus"
        video_format  : "mp4" — from AppConfig; not user-selectable today.
        output_dir    : absolute path string
        """
        return {
            "media_type":    self._type_combo.currentData(),
            "quality_label": self._quality_combo.currentData(),
            "audio_format":  self._audio_format_combo.currentData(),
            "video_format":  self._config.video_format,
            "output_dir":    self._dir_entry.text().strip(),
        }

    def apply_config(self, cfg: AppConfig) -> None:
        """Sync all controls from a (possibly freshly saved) AppConfig."""
        self._syncing_config = True
        try:
            self._dir_entry.setText(cfg.output_dir)
            self._dir_entry.setToolTip(cfg.output_dir)

            type_idx = self._type_combo.findData(cfg.media_type)
            if type_idx >= 0:
                self._type_combo.setCurrentIndex(type_idx)

            audio_fmt_idx = self._audio_format_combo.findData(cfg.audio_format)
            if audio_fmt_idx >= 0:
                self._audio_format_combo.setCurrentIndex(audio_fmt_idx)

            self._update_quality_options(cfg.media_type)

            saved_key = self._saved_quality_id_for_current_controls()
            q_idx = self._quality_combo.findData(saved_key)
            if q_idx >= 0:
                self._quality_combo.setCurrentIndex(q_idx)

            self._clip_switch.setChecked(cfg.clipboard_monitor)
            self._quality_value_lbl.setText(self._quality_combo.currentText())
            self._sync_quality_tooltip()
            self._sync_type_tooltip()
        finally:
            self._syncing_config = False

    def set_directory(self, path: str) -> None:
        self._dir_entry.setText(path)
        self.options_changed.emit()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        # Visible order is Type -> Format -> Quality (right-to-left in
        # Hebrew, left-to-right in English). A QHBoxLayout's first-added
        # child ends up rightmost under RTL (see _make_group's docstring),
        # so the row.addWidget() calls below must run in exactly this
        # order: type, then audio-format, then quality.

        # ── Type (Audio / Video) ─────────────────────────────────────────────
        self._lbl_type = QLabel()
        self._type_value_lbl = QLabel()
        # Populated by _populate_type_combo() right below — each item needs
        # a translated label + stable-value userData pair ("audio"/"video"),
        # not just text, so it's built empty here like the quality combo.
        self._type_group, self._type_combo = self._make_group(
            self._lbl_type, self._type_value_lbl, [])
        self._populate_type_combo()
        # Do NOT force_ltr here: unlike the format/quality combos (Latin
        # technical values), "Audio"/"Video" are translated words and must
        # follow the surrounding RTL layout like the other plain labels.
        self._type_combo.currentTextChanged.connect(self._type_value_lbl.setText)
        self._type_combo.currentTextChanged.connect(self._on_type_change)
        row.addWidget(self._type_group)

        # ── Format (audio output format; hidden in video mode) ──────────────
        self._lbl_audio_format = QLabel()
        self._audio_format_value_lbl = QLabel()
        self._audio_format_group, self._audio_format_combo = self._make_group(
            self._lbl_audio_format, self._audio_format_value_lbl, [])
        self._populate_audio_format_combo()
        force_ltr(self._audio_format_combo)  # codec names are Latin
        self._audio_format_combo.currentTextChanged.connect(self._audio_format_value_lbl.setText)
        self._audio_format_combo.currentTextChanged.connect(self._on_audio_format_change)
        row.addWidget(self._audio_format_group)

        # ── Quality ───────────────────────────────────────────────────────────
        self._lbl_quality = QLabel()
        self._quality_value_lbl = QLabel()
        # Populated by _update_quality_options() (called from apply_config()
        # right after _build() returns) rather than here, since each item
        # needs a translated label + stable-key userData pair, not just text.
        self._quality_group, self._quality_combo = self._make_group(
            self._lbl_quality, self._quality_value_lbl, [])
        force_ltr(self._quality_combo)  # bitrate strings are Latin
        # The visible value label mixes a translated word with a Latin
        # bitrate/resolution (e.g. "הכי טובה · 320 kbps"). Left un-forced it
        # inherits the pill's RTL layoutDirection, and Qt's bidi algorithm
        # then reorders the two runs around the "·" separator — force_ltr
        # on the combo above doesn't reach this *sibling* label.
        force_ltr_label(self._quality_value_lbl)
        self._quality_combo.currentTextChanged.connect(self._quality_value_lbl.setText)
        self._quality_combo.currentTextChanged.connect(self._on_quality_change)
        row.addWidget(self._quality_group)

        # ── Output directory (folder icon + label + path, one chip) ───────────
        self._lbl_save = QLabel()
        self._browse_btn = ToolButton(FluentIcon.FOLDER)
        self._browse_btn.setObjectName("optionsBrowseBtn")
        self._browse_btn.setFixedSize(24, 24)
        self._browse_btn.clicked.connect(self._on_browse)
        self._dir_entry = LineEdit()
        self._dir_entry.setFixedWidth(230)
        self._dir_entry.setFixedHeight(28)
        force_ltr_input(self._dir_entry)  # file path must read L→R
        # Qt scrolls a LineEdit's viewport to keep the cursor (end-of-text,
        # after setText) visible but never inserts an ellipsis, so overflow
        # can clip mid-word. The tooltip surfaces the untruncated path.
        self._dir_entry.textChanged.connect(self._on_dir_text_changed)
        self._dir_entry.editingFinished.connect(self._on_dir_committed)
        self._save_pill = self._make_save_pill()
        row.addWidget(self._save_pill)

        row.addStretch(1)

        # ── Clipboard monitor toggle (pushed to the trailing edge) ────────────
        self._lbl_clip = QLabel()
        row.addWidget(self._lbl_clip)
        self._clip_switch = SwitchButton()
        self._clip_switch.setOnText("")
        self._clip_switch.setOffText("")
        self._clip_switch.setFixedHeight(28)
        self._clip_switch.checkedChanged.connect(self._on_clip_toggle)
        row.addWidget(self._clip_switch)

        self._labels = [self._lbl_type, self._lbl_audio_format, self._lbl_quality,
                        self._lbl_save, self._lbl_clip]
        self._combos = [self._type_combo, self._audio_format_combo, self._quality_combo]
        self._value_labels = [self._type_value_lbl, self._audio_format_value_lbl, self._quality_value_lbl]

        # currentTextChanged only fires on a *change*, so the value labels
        # need one explicit sync for the initial (index-0) selection.
        self._type_value_lbl.setText(self._type_combo.currentText())
        self._audio_format_value_lbl.setText(self._audio_format_combo.currentText())
        self._quality_value_lbl.setText(self._quality_combo.currentText())

        self._retranslate()
        self._apply_theme()

    def _populate_type_combo(self) -> None:
        """Text is translated ("Audio"/"Video"); data is the stable value
        ("audio"/"video") every internal comparison/config write reads via
        currentData() — never currentText()."""
        self._type_combo.addItem(t("options_type_audio"), userData="audio")
        self._type_combo.addItem(t("options_type_video"), userData="video")

    def _populate_audio_format_combo(self) -> None:
        """Text is the uppercase display form ("MP3"); data is the
        lowercase persisted/logical value ("mp3", matching AUDIO_FORMATS,
        config.audio_format, and every yt-dlp/preset string) — logic and
        persistence always go through currentData(), never currentText()."""
        for codec in AUDIO_FORMATS:
            self._audio_format_combo.addItem(codec.upper(), userData=codec)

    def _make_group(
        self, label: QLabel, value_lbl: QLabel, items: list[str],
    ) -> tuple[QFrame, "_AnchoredComboBox"]:
        """One unified bordered pill: muted label, bold value, dropdown arrow
        — in that reading order, right to left (matching "Quality: 1080p ▾").

        qfluentwidgets' ComboBox always paints its dropdown arrow near its
        OWN right edge (hardcoded in its paintEvent — a fixed offset that
        ignores layoutDirection entirely), so it can never sit flush at a
        pill's *left* edge while the combo also shows its own text next to
        it. The only reliable fix: shrink the real combo down to just its
        arrow glyph (text painted transparent), show the selected value in
        a separate plain QLabel instead, and forward clicks on the whole
        pill to the hidden combo's dropdown via _PillFrame.

        The combo is an _AnchoredComboBox anchored to this pill frame, so
        its popup opens flush under the *pill* (what the user actually
        sees and clicks) instead of under its own 26px arrow-only sliver —
        see _AnchoredComboBox for why that distinction matters.

        In this RTL bar a QHBoxLayout's first-added child ends up rightmost,
        so add order is label, value, combo — giving (right to left): label,
        value, arrow — with the arrow as the true leftmost pixel. (This is
        the order *within* one pill; see _build() for the order *between*
        pills.)
        """
        group = _PillFrame()
        group.setObjectName("optionPill")
        group.setFixedHeight(34)
        group.setCursor(Qt.CursorShape.PointingHandCursor)
        # Qt delivers a click to whichever child widget is under the cursor,
        # not to the parent frame — without this, clicking the label/value
        # text would do nothing since a plain QLabel silently consumes the
        # event instead of it ever reaching _PillFrame.mouseReleaseEvent.
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        value_lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        combo = _AnchoredComboBox(group)
        combo.addItems(items)
        combo.setFixedWidth(_ARROW_ONLY_WIDTH)
        group._target = combo

        lay = QHBoxLayout(group)
        lay.setContentsMargins(4, 0, 10, 0)
        lay.setSpacing(4)
        lay.addWidget(label)
        lay.addWidget(value_lbl)
        lay.addWidget(combo)
        return group, combo

    def _make_save_pill(self) -> QFrame:
        pill = QFrame()
        pill.setObjectName("optionPill")
        pill.setFixedHeight(34)
        lay = QHBoxLayout(pill)
        lay.setContentsMargins(6, 0, 12, 0)
        lay.setSpacing(7)
        lay.addWidget(self._browse_btn)
        lay.addWidget(self._lbl_save)
        lay.addWidget(self._dir_entry)
        return pill

    def _retranslate(self) -> None:
        """Set all translatable label texts. Called from ``_build`` so the
        ``_retranslate()`` naming convention is in place for a future
        live-language-switch upgrade (see ui/i18n.py LanguageManager)."""
        # Inside the pill the label is a compact prefix, so the trailing
        # colon that reads well in a "Label: value" row is dropped.
        self._lbl_type.setText(t("options_type_label").rstrip(":").strip())
        self._lbl_audio_format.setText(t("options_format_label").rstrip(":").strip())
        self._lbl_quality.setText(t("options_quality_label").rstrip(":").strip())
        self._lbl_save.setText(t("options_save_label"))
        self._lbl_clip.setText(t("options_clipboard_label").rstrip(":").strip())
        self._audio_format_combo.setToolTip(t("options_format_label"))
        # dir_entry's tooltip always mirrors the live path (set in
        # apply_config / _on_dir_text_changed), not the static label.
        self._browse_btn.setToolTip(t("options_save_label"))
        self._clip_switch.setToolTip(t("options_clipboard_label"))
        self._sync_quality_tooltip()
        self._sync_type_tooltip()

    # ── Theme ──────────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        c = get_colors()
        accent = c.accent

        # qfluentwidgets controls (ComboBox, LineEdit, ToolButton, ...) set
        # their own stylesheet directly on themselves in __init__. Qt's
        # cascade always gives a widget's own stylesheet priority over an
        # ancestor's — so any rule aimed at them through a *parent* frame's
        # descendant selector (e.g. "QFrame#x ComboBox {...}") is silently
        # ignored, no matter how specific the selector looks. Every control
        # below must therefore be styled via setStyleSheet() on the control
        # itself. Plain QFrame/QLabel have no such competing stylesheet, so
        # ancestor-level selectors for those are fine.
        self.setStyleSheet("OptionsBar { background: transparent; border: none; }")

        # One shared pill style for ALL four chips (type/format/quality/save-to)
        # — a plain QFrame, so ancestor-level styling via objectName is safe and
        # reliable (no competing inline stylesheet like qfluentwidgets controls
        # have). Only border-color shifts on hover — a subtle highlight, never
        # a full fill.
        pill_qss = f"""
            QFrame#optionPill {{
                background: {c.surface};
                border: 1px solid {c.border};
                border-radius: 17px;
            }}
            QFrame#optionPill:hover {{
                border-color: {accent};
            }}
        """
        for pill in (self._type_group, self._audio_format_group, self._quality_group, self._save_pill):
            pill.setStyleSheet(pill_qss)

        group_label_qss = f"color: {c.text_tertiary}; background: transparent; font-size: 12px;"
        for lbl in (self._lbl_type, self._lbl_audio_format, self._lbl_quality, self._lbl_save):
            lbl.setStyleSheet(group_label_qss)

        value_lbl_qss = f"color: {c.text_primary}; background: transparent; font-size: 12px; font-weight: 600;"
        for lbl in self._value_labels:
            lbl.setStyleSheet(value_lbl_qss)

        # The real combo now shows only its own dropdown-arrow glyph (its
        # text is painted transparent — the value is displayed by the plain
        # QLabel next to it instead, see _make_group), so its background
        # must stay fully transparent too — every pseudo-state below
        # explicitly repeats "background: transparent" rather than leaving
        # it unset, because Qt can fall back to the application palette's
        # Highlight colour (which qfluentwidgets keeps pinned to the current
        # accent) for any state a stylesheet doesn't fully specify — that
        # fallback is what previously painted the whole control solid accent
        # on hover instead of just a subtle highlight.
        combo_qss = """
            ComboBox {
                background: transparent;
                border: none;
                color: transparent;
                padding: 0;
                min-height: 32px;
            }
            ComboBox:hover, ComboBox:pressed, ComboBox:focus, ComboBox:!hover {
                background: transparent;
                border: none;
                color: transparent;
            }
        """
        for combo in self._combos:
            combo.setStyleSheet(combo_qss)

        self._dir_entry.setStyleSheet(f"""
            LineEdit {{
                background: transparent;
                border: none;
                color: {c.text_primary};
                font-size: 12px;
                padding: 0;
            }}
        """)
        self._browse_btn.setStyleSheet(f"""
            ToolButton {{
                background: transparent;
                border: none;
                border-radius: 8px;
                color: {c.text_secondary};
                font-size: 14px;
            }}
            ToolButton:hover {{
                background: {c.surface2};
                color: {accent};
            }}
        """)

        clip_lbl_style = f"color: {c.text_secondary}; font-size: 12px; font-weight: 600; background: transparent;"
        self._lbl_clip.setStyleSheet(clip_lbl_style)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _update_quality_options(self, media_type: str) -> None:
        if self._quality_combo.dropMenu:
            self._quality_combo._closeComboMenu()  # noqa: SLF001 - avoid stale menu actions after clear()
        self._quality_combo.blockSignals(True)
        self._quality_combo.clear()
        if media_type == "audio":
            codec = self._audio_format_combo.currentData() or self._config.audio_format
            items = audio_presets_for_codec(codec)
            saved_key = self._saved_audio_quality_id(codec)
            audio_format_visible = True
        else:
            items = VIDEO_QUALITY_PRESETS
            saved_key = coerce_video_quality_id(self._config.video_quality)
            audio_format_visible = False
        for preset in items:
            self._quality_combo.addItem(
                quality_display(preset.label_key, preset.detail_key),
                userData=preset.id,
            )
        self._sync_quality_value_width()
        # Restore the last quality the user picked for this media type, so
        # flipping audio -> video -> audio doesn't silently reset the choice.
        saved_idx = self._quality_combo.findData(saved_key)
        if saved_idx >= 0:
            self._quality_combo.setCurrentIndex(saved_idx)
        elif self._quality_combo.count() > 0:
            self._quality_combo.setCurrentIndex(0)
        # The audio-format selector only applies to audio; hide the whole
        # chip for video (video's output format is fixed at MP4 today).
        self._audio_format_group.setVisible(audio_format_visible)
        self._audio_format_combo.setEnabled(audio_format_visible)
        self._quality_combo.blockSignals(False)
        # blockSignals suppressed currentTextChanged above, so the value
        # label (which mirrors it) needs an explicit sync here too.
        self._quality_value_lbl.setText(self._quality_combo.currentText())
        self._sync_quality_tooltip()

    def _sync_quality_value_width(self) -> None:
        """Fit the quality pill to the current media/codec choices.

        Video labels can be noticeably wider than MP3 bitrate labels, so
        reserving one global width makes audio mode look too airy.
        """
        metrics = self._quality_value_lbl.fontMetrics()
        widest = max(
            (
                metrics.horizontalAdvance(self._quality_combo.itemText(i))
                for i in range(self._quality_combo.count())
            ),
            default=0,
        )
        self._quality_value_lbl.setMinimumWidth(widest)

    def _on_type_change(self, _label: str) -> None:
        media_type = self._type_combo.currentData()
        self._update_quality_options(media_type)
        self._sync_type_tooltip()
        if self._syncing_config:
            return
        if media_type in ("audio", "video") and media_type != self._config.media_type:
            self._config.media_type = media_type
            self._config.save()
        self.options_changed.emit()

    def _on_quality_change(self, _label: str) -> None:
        key = self._quality_combo.currentData()
        self._sync_quality_tooltip()
        if self._syncing_config:
            return
        if key:
            if self._type_combo.currentData() == "audio":
                if key != self._config.audio_quality:
                    self._config.audio_quality = key
                    by_codec = self._config.audio_quality_by_codec
                    by_codec[self._audio_format_combo.currentData()] = key
                    self._config.audio_quality_by_codec = by_codec
                    self._config.save()
            elif key != self._config.video_quality:
                self._config.video_quality = key
                self._config.save()
        self.options_changed.emit()

    def _on_audio_format_change(self, _label: str) -> None:
        codec = self._audio_format_combo.currentData()
        if self._type_combo.currentData() == "audio":
            self._update_quality_options("audio")
        if self._syncing_config:
            return
        if codec and codec != self._config.audio_format:
            by_codec = self._config.audio_quality_by_codec
            current_key = self._quality_combo.currentData()
            if current_key:
                by_codec[codec] = current_key
            self._config.audio_format = codec
            if current_key:
                self._config.audio_quality = current_key
            self._config.audio_quality_by_codec = by_codec
            self._config.save()
        self.options_changed.emit()

    def _saved_quality_id_for_current_controls(self) -> str:
        if self._type_combo.currentData() == "audio":
            return self._saved_audio_quality_id(self._audio_format_combo.currentData())
        return coerce_video_quality_id(self._config.video_quality)

    def _saved_audio_quality_id(self, codec: str) -> str:
        codec = codec or "mp3"
        by_codec = self._config.audio_quality_by_codec
        if codec in by_codec:
            return coerce_audio_quality_id(by_codec[codec], codec)
        return coerce_audio_quality_id(self._config.audio_quality, codec)

    def _sync_quality_tooltip(self) -> None:
        key = self._quality_combo.currentData()
        tooltip_key = "options_quality_label"
        if self._type_combo.currentData() == "audio":
            codec = self._audio_format_combo.currentData() or self._config.audio_format
            preset = next((p for p in audio_presets_for_codec(codec) if p.id == key), None)
            if preset is not None:
                tooltip_key = preset.tooltip_key
        else:
            preset = next((p for p in VIDEO_QUALITY_PRESETS if p.id == key), None)
            if preset is not None and preset.tooltip_key:
                tooltip_key = preset.tooltip_key
        tooltip = t(tooltip_key)
        self._quality_group.setToolTip(tooltip)
        self._quality_combo.setToolTip(tooltip)
        self._quality_value_lbl.setToolTip(tooltip)

    def _sync_type_tooltip(self) -> None:
        """Video mode has no visible output-format selector (MP4 is the
        only supported video format today), so the Type pill's tooltip
        carries that information instead — a hint, not visual clutter."""
        if self._type_combo.currentData() == "video":
            tooltip = t("options_video_format_note")
        else:
            tooltip = t("options_type_label")
        self._type_group.setToolTip(tooltip)
        self._type_combo.setToolTip(tooltip)
        self._type_value_lbl.setToolTip(tooltip)

    def _on_dir_text_changed(self, text: str) -> None:
        self._dir_entry.setToolTip(text)
        self.options_changed.emit()

    def _on_browse(self) -> None:
        current = self._dir_entry.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(
            self, t("choose_download_folder"), current,
        )
        if path:
            self._dir_entry.setText(path)
            self._config.output_dir = path
            self._config.save()
            self.options_changed.emit()

    def _on_dir_committed(self) -> None:
        """Persist a manually-typed output directory to config.

        The path is not validated here — DownloadController.start_batch
        runs the writability check before any download starts.
        """
        path = self._dir_entry.text().strip()
        if path and path != self._config.output_dir:
            self._config.output_dir = path
            self._config.save()

    def _on_clip_toggle(self, checked: bool) -> None:
        self._config.clipboard_monitor = checked
        self._config.save()
        self.options_changed.emit()
