"""
tests/test_status_bar.py  –  Controlled status-state footer tests
===================================================================
Covers the Part-1/2/6 behaviour of the rewritten StatusBar:

  * idle has no "Ready." text and a hidden progress bar
  * active (indeterminate/determinate) states persist
  * temporary messages auto-clear; a newer message cancels a pending clear
  * error summaries never auto-clear
  * fetch → indeterminate mode; download → determinate mode
  * completion resets/hides the bar
  * pause/cancelling preserve the current progress (no jump to 0 or 100)

Headless (QT_QPA_PLATFORM=offscreen); skips when PySide6 is missing.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from ui.panels.status_bar import StatusBar, StatusState
    from ui.components.status_icon import StatusKind
    from core.batch_progress import BatchProgressAggregator
except ImportError:  # pragma: no cover
    pytest.skip("PySide6 / qfluentwidgets not available", allow_module_level=True)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bar(app):
    b = StatusBar()
    # Show so child-widget isVisible() reflects the explicit setVisible state
    # (Qt reports a child invisible while any ancestor is unshown).
    b.show()
    app.processEvents()
    yield b
    b.deleteLater()


def _snapshot(progress=0.34, done=17, total=50, speed=2_000_000.0, eta=1000.0):
    a = BatchProgressAggregator(speed_smoothing=1.0)
    a.reset([str(i) for i in range(total)])
    # Drive to the requested aggregate progress with byte-weighted jobs.
    for i in range(total):
        if i < done:
            a.complete(str(i), final_bytes=1_000_000)
        elif i == done:
            a.update(str(i), downloaded_bytes=int(1_000_000 * ((progress * total) - done)),
                     total_bytes=1_000_000, speed_bps=speed, eta_seconds=eta)
    return a.snapshot()


# ── Idle ────────────────────────────────────────────────────────────────────

class TestIdle:
    def test_idle_has_no_ready_text(self, bar):
        bar.reset_to_idle()
        assert bar._status_lbl.text() == ""
        assert "Ready" not in bar._status_lbl.text()

    def test_idle_progress_bar_hidden(self, bar):
        bar.reset_to_idle()
        assert not bar._det_bar.isVisible()
        assert not bar._ind_bar.isVisible()

    def test_idle_hides_cancel_and_clears_metrics(self, bar):
        bar.reset_to_idle()
        assert not bar._cancel_btn.isVisible()
        assert bar._speed_lbl.text() == ""
        assert bar._eta_lbl.text() == ""

    def test_starts_idle(self, bar):
        assert bar.state == StatusState.IDLE
        assert bar._status_lbl.text() == ""


# ── Indeterminate ───────────────────────────────────────────────────────────

class TestIndeterminate:
    def test_fetch_starts_indeterminate_mode(self, bar):
        bar.show_indeterminate("Fetching information…")
        assert bar.state == StatusState.INDETERMINATE
        assert bar._ind_bar.isVisible()
        assert not bar._det_bar.isVisible()
        assert bar._status_lbl.text() == "Fetching information…"
        assert bar._cancel_btn.isVisible()

    def test_indeterminate_message_persists(self, bar, app):
        bar.show_indeterminate("Scanning page for media…")
        QTest.qWait(80)
        app.processEvents()
        # No auto-clear: an active operation message stays put.
        assert bar.state == StatusState.INDETERMINATE
        assert bar._status_lbl.text() == "Scanning page for media…"


# ── Determinate batch progress ──────────────────────────────────────────────

class TestBatchProgress:
    def test_download_uses_determinate_mode(self, bar):
        bar.show_batch_progress(_snapshot())
        assert bar.state == StatusState.DOWNLOADING
        assert bar._det_bar.isVisible()
        assert not bar._ind_bar.isVisible()
        assert bar._cancel_btn.isVisible()

    def test_switching_modes_never_shows_both_bars(self, bar):
        bar.show_indeterminate("fetch")
        bar.show_batch_progress(_snapshot())
        assert bar._det_bar.isVisible()
        assert not bar._ind_bar.isVisible()
        bar.show_indeterminate("fetch again")
        assert bar._ind_bar.isVisible()
        assert not bar._det_bar.isVisible()

    def test_progress_value_reflects_snapshot(self, bar):
        bar.show_batch_progress(_snapshot(progress=0.5, done=25, total=50))
        assert bar._det_bar.value() == 50

    def test_speed_and_eta_shown(self, bar):
        bar.show_batch_progress(_snapshot())
        assert "MB/s" in bar._speed_lbl.text()
        assert bar._eta_lbl.text() != ""


# ── Temporary messages ──────────────────────────────────────────────────────

class TestTemporary:
    def test_temporary_message_clears_after_timeout(self, bar, app):
        bar.show_temporary("50 downloads completed.", StatusKind.SUCCESS, duration_ms=60)
        assert bar.state == StatusState.TEMPORARY
        assert bar._status_lbl.text() == "50 downloads completed."
        QTest.qWait(140)
        app.processEvents()
        assert bar.state == StatusState.IDLE
        assert bar._status_lbl.text() == ""

    def test_new_message_cancels_pending_clear(self, bar, app):
        # Old temporary about to clear…
        bar.show_temporary("old message", StatusKind.SUCCESS, duration_ms=60)
        # …a newer active operation arrives before the timer fires.
        bar.show_indeterminate("new operation")
        QTest.qWait(140)
        app.processEvents()
        # The stale timer must NOT have wiped the newer message.
        assert bar.state == StatusState.INDETERMINATE
        assert bar._status_lbl.text() == "new operation"

    def test_completion_resets_and_hides_bar(self, bar, app):
        bar.show_batch_progress(_snapshot())
        assert bar._det_bar.isVisible()
        bar.show_temporary("done", StatusKind.SUCCESS, duration_ms=40)
        # Temporary state already hides the determinate bar.
        assert not bar._det_bar.isVisible()
        QTest.qWait(120)
        app.processEvents()
        assert bar.state == StatusState.IDLE
        assert not bar._det_bar.isVisible()


# ── Error summary ───────────────────────────────────────────────────────────

class TestErrorSummary:
    def test_error_summary_does_not_auto_clear(self, bar, app):
        bar.show_error_summary("Downloads stopped because of an error.")
        assert bar.state == StatusState.ERROR
        QTest.qWait(120)
        app.processEvents()
        # Critical error must remain until explicitly cleared.
        assert bar.state == StatusState.ERROR
        assert bar._status_lbl.text() == "Downloads stopped because of an error."

    def test_error_hides_progress_and_cancel(self, bar):
        bar.show_error_summary("boom")
        assert not bar._det_bar.isVisible()
        assert not bar._ind_bar.isVisible()
        assert not bar._cancel_btn.isVisible()


# ── Pause / cancelling preserve progress ────────────────────────────────────

class TestPauseCancelling:
    def test_pause_preserves_progress(self, bar):
        bar.show_batch_progress(_snapshot(progress=0.4, done=20, total=50))
        value = bar._det_bar.value()
        bar.show_paused()
        assert bar.state == StatusState.PAUSED
        assert bar._det_bar.value() == value  # not reset to 0 or 100
        assert not bar._cancel_btn.isVisible()

    def test_cancelling_preserves_progress(self, bar):
        bar.show_batch_progress(_snapshot(progress=0.4, done=20, total=50))
        value = bar._det_bar.value()
        bar.show_cancelling()
        assert bar.state == StatusState.CANCELLING
        assert bar._det_bar.value() == value
        assert bar._det_bar.value() not in (0, 100)


# ── Offline routing ─────────────────────────────────────────────────────────

class TestOffline:
    def test_offline_without_message_is_quiet(self, bar):
        # AppWindow routes offline to the OfflineBanner; the footer stays quiet.
        bar.show_offline()
        assert bar._status_lbl.text() == ""
