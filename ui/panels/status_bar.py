"""
ui/panels/status_bar.py  –  Controlled status-state footer
============================================================
A fixed-height bar pinned to the bottom of the main window. Historically
every controller poked at a single ``_status_lbl`` (and the progress bar,
speed, ETA and cancel button) in whatever order it liked, so an old timer
could erase a newer message and a fetch "Ready." could sit on screen
forever.

This rewrite replaces that free-for-all with an explicit **state model**.
Callers no longer set text/progress/metrics independently; they call one
semantic method per situation and the bar fully defines what each state
looks like:

    show_idle() / reset_to_idle()   nothing running — empty footer
    show_indeterminate(message)     work of unknown length (fetch/scan)
    show_batch_progress(snapshot)   determinate whole-batch download
    show_paused()                   user paused (progress preserved)
    show_cancelling()               shutdown in progress (progress preserved)
    show_temporary(message, kind)   brief terminal note; auto-clears
    show_error_summary(message)     persistent, actionable-error summary
    show_offline() / show_online()  connectivity (paired with OfflineBanner)

Each state defines, in one place: the text, the status icon, the progress
mode + visibility, speed/ETA visibility, cancel visibility, and whether it
auto-clears. A single shared timer drives auto-clear, and starting any new
state cancels a pending clear so an old timer can never wipe a newer
message.

Signals
-------
cancel_requested()   User clicked Cancel.
"""

from __future__ import annotations

import math
import time
from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel,
    QSizePolicy, QVBoxLayout, QWidget,
)
from qfluentwidgets import IndeterminateProgressBar, ProgressBar, ToolButton

from ui.components.status_icon import StatusIcon, StatusKind
from ui.direction import isolate_number
from ui.i18n import t
from ui.theme_manager import ThemeManager, get_colors
from utils.time_format import seconds_to_str

_ERROR = "#f87171"

# Auto-clear delay for temporary terminal messages (Part 1: ~4–6 s).
_TEMPORARY_MS = 5000

# How often the ETA re-renders between batch snapshots, so the number visibly
# counts down instead of holding still until the next snapshot arrives.
_ETA_TICK_MS = 1000


class StatusState(Enum):
    """Every distinct footer state. Transitions go through _apply_state()."""
    IDLE            = auto()
    INDETERMINATE   = auto()   # fetch / scan / channel import / starting
    DOWNLOADING     = auto()   # determinate batch progress
    PAUSED          = auto()
    CANCELLING      = auto()
    TEMPORARY       = auto()   # brief terminal message; auto-clears
    ERROR           = auto()   # persistent actionable-error summary
    OFFLINE         = auto()


def _fmt_speed(bps: Optional[float]) -> str:
    if not bps or bps <= 0:
        return ""
    if bps >= 1_048_576:
        return f"{bps / 1_048_576:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


# ──────────────────────────────────────────────────────────────────────────────
# StatusBar
# ──────────────────────────────────────────────────────────────────────────────

class StatusBar(QFrame):
    """Bottom-anchored, state-driven status footer."""

    cancel_requested = Signal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._state = StatusState.IDLE
        self._indeterminate = False

        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.timeout.connect(self.reset_to_idle)

        # Live ETA countdown. Batch snapshots arrive a couple of times a second
        # at best and stop entirely between tracks, so without this the number
        # sat frozen. The widget still computes no batch maths: it holds the
        # last ETA the aggregator gave it plus the moment it arrived, and simply
        # renders that value minus the time since.
        self._eta_base_seconds: Optional[float] = None
        self._eta_lower_base: Optional[float] = None
        self._eta_upper_base: Optional[float] = None
        self._eta_confidence: str = "warming"
        self._eta_base_at: float = 0.0
        self._eta_timer = QTimer(self)
        self._eta_timer.setInterval(_ETA_TICK_MS)
        self._eta_timer.timeout.connect(self._tick_eta)

        self._build()

        tm = ThemeManager.instance()
        if tm is not None:
            tm.theme_changed.connect(self._apply_theme)

        self._apply_state(StatusState.IDLE)

    # ── Public state (previous state) ──────────────────────────────────────────

    @property
    def state(self) -> StatusState:
        return self._state

    # ── Semantic API ───────────────────────────────────────────────────────────

    def show_idle(self) -> None:
        """Alias for reset_to_idle()."""
        self.reset_to_idle()

    def reset_to_idle(self) -> None:
        """Empty footer: no text, no bar, no metrics, no cancel."""
        self._cancel_clear_timer()
        self._apply_state(StatusState.IDLE)

    def show_indeterminate(
        self, message: str, kind: StatusKind = StatusKind.ACTIVITY
    ) -> None:
        """Work whose total is unknown (fetching, scanning, starting)."""
        self._cancel_clear_timer()
        self._apply_state(StatusState.INDETERMINATE, message=message, icon=kind)

    def show_batch_progress(self, snapshot, message: Optional[str] = None) -> None:
        """
        Determinate whole-batch download progress.

        ``snapshot`` is a core.batch_progress.BatchSnapshot (duck-typed here:
        the widget only *renders* the numbers, it never computes them).
        """
        self._cancel_clear_timer()
        pct = int(round(max(0.0, min(1.0, snapshot.progress)) * 100))
        if message is None:
            message = t(
                "status_downloading_progress",
                done=snapshot.completed,
                total=snapshot.total,
                pct=pct,
            )
        speed = _fmt_speed(snapshot.speed_bps)
        # Re-anchor the countdown on every snapshot: this value is authoritative
        # and the ticker only fills the gaps between them.
        self._eta_base_seconds = snapshot.eta_seconds
        self._eta_lower_base = getattr(snapshot, "eta_lower_seconds", None)
        self._eta_upper_base = getattr(snapshot, "eta_upper_seconds", None)
        self._eta_confidence = getattr(snapshot, "eta_confidence", "warming")
        self._eta_base_at = time.monotonic()
        eta = self._fmt_eta_range(
            snapshot.eta_seconds,
            self._eta_lower_base,
            self._eta_upper_base,
            self._eta_confidence,
        )
        self._apply_state(
            StatusState.DOWNLOADING,
            message=message,
            icon=StatusKind.ACTIVITY,
            fraction=snapshot.progress,
            speed=speed,
            eta=eta,
        )

    def show_paused(self, fraction: Optional[float] = None) -> None:
        """User paused — preserve the current progress, present resume elsewhere."""
        self._cancel_clear_timer()
        self._apply_state(
            StatusState.PAUSED,
            message=t("status_paused"),
            icon=StatusKind.PAUSED,
            fraction=fraction,   # None => keep current bar value
        )

    def show_cancelling(self) -> None:
        """Shutdown in progress — keep the real progress, no metrics."""
        self._cancel_clear_timer()
        self._apply_state(
            StatusState.CANCELLING,
            message=t("status_cancelling"),
            icon=StatusKind.CANCELLING,
            fraction=None,       # keep current bar value
        )

    def show_temporary(
        self,
        message: str,
        kind: StatusKind = StatusKind.SUCCESS,
        duration_ms: int = _TEMPORARY_MS,
    ) -> None:
        """Brief terminal note (e.g. '50 downloads completed.') that clears itself."""
        self._cancel_clear_timer()
        self._apply_state(StatusState.TEMPORARY, message=message, icon=kind)
        self._clear_timer.start(duration_ms)

    def show_error_summary(self, message: str) -> None:
        """Persistent error summary — never auto-clears. The primary error UI
        (dialog/InfoBar) carries the actionable detail; this is the footer echo."""
        self._cancel_clear_timer()
        self._apply_state(StatusState.ERROR, message=message, icon=StatusKind.ERROR)

    def show_offline(self, message: str = "") -> None:
        """Connectivity lost. Paired with OfflineBanner (which owns the detail);
        the footer stays quiet unless a caller passes an explicit short line."""
        self._cancel_clear_timer()
        self._apply_state(
            StatusState.OFFLINE,
            message=message,
            icon=StatusKind.OFFLINE if message else StatusKind.NONE,
        )

    def show_online(self, message: str = "") -> None:
        """Connectivity restored — optional brief confirmation, then idle."""
        if message:
            self.show_temporary(message, kind=StatusKind.SUCCESS)
        else:
            self.reset_to_idle()

    # ── State application (single source of truth for the whole footer) ────────

    def _apply_state(
        self,
        state: StatusState,
        *,
        message: str = "",
        icon: StatusKind = StatusKind.NONE,
        fraction: Optional[float] = None,
        speed: str = "",
        eta: str = "",
    ) -> None:
        self._state = state

        # Text + icon
        self._status_lbl.setText(message)
        self._icon.set_kind(icon)

        # Progress mode + visibility
        if state == StatusState.INDETERMINATE:
            self._start_indeterminate()
        elif state == StatusState.DOWNLOADING:
            self._set_determinate(fraction if fraction is not None else 0.0)
        elif state in (StatusState.PAUSED, StatusState.CANCELLING):
            # Preserve the current bar value (do not reset to 0 or 100).
            self._stop_indeterminate()
            if fraction is not None:
                self._set_determinate(fraction)
            # else: leave the determinate bar exactly where it was
        else:   # IDLE / TEMPORARY / ERROR / OFFLINE
            self._stop_indeterminate()
            self._det_bar.setValue(0)
            self._det_bar.setVisible(False)

        # Metrics
        self._speed_lbl.setText(speed)
        self._eta_lbl.setText(eta)
        self._eta_lbl.setToolTip(t("eta_tooltip") if eta else "")

        # Any state other than a live download has no ETA to count down.
        if state != StatusState.DOWNLOADING:
            self._eta_base_seconds = None
            self._eta_lower_base = None
            self._eta_upper_base = None
        self._sync_eta_timer()

        # Cancel button — only while an interruptible operation is live.
        self._cancel_btn.setVisible(
            state in (StatusState.INDETERMINATE, StatusState.DOWNLOADING)
        )

    # ── ETA wording (localized, clearly an estimate) ───────────────────────────

    def _fmt_eta(self, eta_seconds: Optional[float]) -> str:
        """Render a remaining-time value, or the "calculating" placeholder.

        Seconds-granular. The previous version rounded to whole minutes above
        one minute, so the string only changed every ~30 s and never read as a
        countdown. ``seconds_to_str`` is the app-wide duration formatter, and
        the result is bidi-isolated so its digits and colons keep their order
        inside Hebrew prose.
        """
        if eta_seconds is None:
            return t("eta_calculating")
        s = int(round(max(0.0, eta_seconds)))
        return t("eta_about_left", time=isolate_number(seconds_to_str(s)))

    @staticmethod
    def _rounded_interval(seconds: float, *, upper: bool) -> int:
        seconds = max(0.0, seconds)
        # The interval itself communicates uncertainty; keep its moving edges
        # seconds-granular so a heartbeat never looks frozen. Only hour-scale
        # bounds are coarsened, where seconds are genuinely immaterial.
        step = 1 if seconds < 3600 else 15
        units = seconds / step
        rounded = math.ceil(units) if upper else math.floor(units)
        return int(max(step if upper and seconds > 0 else 0, rounded * step))

    def _fmt_eta_range(
        self,
        center: Optional[float],
        lower: Optional[float],
        upper: Optional[float],
        confidence: str,
    ) -> str:
        if center is None:
            return t("eta_calculating")
        if confidence == "current_speed":
            current = self._rounded_interval(center, upper=True)
            return t(
                "eta_current_speed_left",
                time=isolate_number(seconds_to_str(current)),
            )
        if lower is None:
            return self._fmt_eta(center)
        low = self._rounded_interval(lower, upper=False)
        if upper is None:
            return self._fmt_eta(center)
        high = max(low, self._rounded_interval(upper, upper=True))
        return t(
            "eta_range_left",
            low=isolate_number(seconds_to_str(low)),
            high=isolate_number(seconds_to_str(high)),
        )

    def _tick_eta(self) -> None:
        """Advance the displayed ETA one second between snapshots."""
        if self._state != StatusState.DOWNLOADING or self._eta_base_seconds is None:
            self._eta_timer.stop()
            return
        elapsed = max(0.0, time.monotonic() - self._eta_base_at)
        # Floors at zero rather than going negative: an overrun means the batch
        # is running behind the estimate, which the next snapshot will correct.
        lower = (
            max(0.0, self._eta_lower_base - elapsed)
            if self._eta_lower_base is not None else None
        )
        upper = (
            max(0.0, self._eta_upper_base - elapsed)
            if self._eta_upper_base is not None else None
        )
        self._eta_lbl.setText(self._fmt_eta_range(
            max(0.0, self._eta_base_seconds - elapsed),
            lower,
            upper,
            self._eta_confidence,
        ))

    def _sync_eta_timer(self) -> None:
        """Run the countdown only while a live batch actually has an ETA.

        Stopped outside DOWNLOADING so a paused, cancelling or finished batch
        never appears to keep counting down, and stopped when the ETA is
        ``None`` so "calculating…" is not treated as a number.
        """
        should_run = (
            self._state == StatusState.DOWNLOADING
            and self._eta_base_seconds is not None
        )
        if should_run:
            if not self._eta_timer.isActive():
                self._eta_timer.start()
        elif self._eta_timer.isActive():
            self._eta_timer.stop()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        # ── Progress bar row — a fixed-height slot that is ALWAYS present so
        # showing/hiding a bar never changes the footer height (keeps the
        # Download button from jumping). ──────────────────────────────────────
        self._bar_slot = QWidget()
        self._bar_slot.setFixedHeight(3)
        self._bar_slot.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        slot_layout = QVBoxLayout(self._bar_slot)
        slot_layout.setContentsMargins(0, 0, 0, 0)
        slot_layout.setSpacing(0)

        self._det_bar = ProgressBar()
        self._det_bar.setFixedHeight(3)
        self._det_bar.setRange(0, 100)
        self._det_bar.setValue(0)
        self._det_bar.setTextVisible(False)
        self._det_bar.setVisible(False)
        slot_layout.addWidget(self._det_bar)

        self._ind_bar = IndeterminateProgressBar()
        self._ind_bar.setFixedHeight(3)
        self._ind_bar.setVisible(False)
        slot_layout.addWidget(self._ind_bar)

        root.addWidget(self._bar_slot)

        # ── Text / metrics row ────────────────────────────────────────────────
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        self._icon = StatusIcon(size=15)
        self._icon.setVisible(False)
        bottom_row.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)

        self._status_lbl = QLabel("")
        self._status_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bottom_row.addWidget(self._status_lbl)

        self._speed_lbl = QLabel("")
        self._speed_lbl.setFixedWidth(90)
        # AlignTrailing follows layout direction so the metric sits at the
        # bar's trailing edge in both LTR and RTL.
        self._speed_lbl.setAlignment(Qt.AlignmentFlag.AlignTrailing | Qt.AlignmentFlag.AlignVCenter)
        bottom_row.addWidget(self._speed_lbl)

        self._eta_lbl = QLabel("")
        # Wide enough for the longest center, range, or lower-bound string in
        # either locale. The previous 120px silently clipped even the old
        # "Calculating time remaining…" placeholder. Fixed rather than minimum
        # so a changing duration never shifts the rest of the footer.
        # tests/test_status_bar.py measures this against the real font metrics.
        self._eta_lbl.setFixedWidth(320)
        self._eta_lbl.setAlignment(Qt.AlignmentFlag.AlignTrailing | Qt.AlignmentFlag.AlignVCenter)
        bottom_row.addWidget(self._eta_lbl)

        self._cancel_btn = ToolButton()
        self._cancel_btn.setText(t("cancel"))
        self._cancel_btn.setFixedSize(70, 26)
        self._cancel_btn.setVisible(False)
        self._cancel_btn.setToolTip(t("cancel"))
        self._cancel_btn.clicked.connect(self.cancel_requested)
        bottom_row.addWidget(self._cancel_btn)

        root.addLayout(bottom_row)

        self._apply_theme()

    # ── Theme ──────────────────────────────────────────────────────────────────

    def _apply_theme(self) -> None:
        c = get_colors()

        self.setStyleSheet("background: transparent; border: none;")

        self._det_bar.setStyleSheet(f"""
            ProgressBar {{
                background: {c.border};
                border: none;
                border-radius: 2px;
            }}
            ProgressBar::chunk {{
                background: {c.accent};
                border-radius: 2px;
            }}
        """)

        self._status_lbl.setStyleSheet(
            f"color: {c.text_secondary}; font-size: 12px; background: transparent;"
        )
        self._speed_lbl.setStyleSheet(
            f"color: {c.text_tertiary}; font-size: 11px; background: transparent;"
        )
        self._eta_lbl.setStyleSheet(
            f"color: {c.text_tertiary}; font-size: 11px; background: transparent;"
        )

        self._cancel_btn.setStyleSheet(f"""
            ToolButton {{
                background: transparent;
                border: 1px solid {c.border};
                border-radius: 6px;
                color: {c.text_secondary};
                font-size: 11px;
            }}
            ToolButton:hover {{
                border-color: {_ERROR};
                color: {_ERROR};
            }}
        """)

    # ── Internal progress-mode helpers ─────────────────────────────────────────

    def _set_determinate(self, fraction: float) -> None:
        if self._indeterminate:
            self._stop_indeterminate()
        value = int(max(0.0, min(1.0, fraction)) * 100)
        self._det_bar.setValue(value)
        # Determinate bar is hidden at exactly zero while idle, but visible
        # during a live download even at 0% so the user sees it engage.
        self._det_bar.setVisible(True)

    def _start_indeterminate(self) -> None:
        self._det_bar.setVisible(False)
        if not self._indeterminate:
            self._indeterminate = True
            self._ind_bar.setVisible(True)
            self._ind_bar.start()

    def _stop_indeterminate(self) -> None:
        if self._indeterminate:
            self._ind_bar.stop()
            self._ind_bar.setVisible(False)
            self._indeterminate = False

    # ── Timer helper ───────────────────────────────────────────────────────────

    def _cancel_clear_timer(self) -> None:
        if self._clear_timer.isActive():
            self._clear_timer.stop()
