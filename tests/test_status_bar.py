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

import dataclasses
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


def _snapshot(
    progress=0.34,
    done=17,
    total=50,
    speed=2_000_000.0,
    eta=1000.0,
    *,
    eta_lower=None,
    eta_upper=None,
    eta_confidence="warming",
):
    """A real BatchSnapshot with a chosen batch ETA.

    `eta` is set on the snapshot directly rather than fed to a job, because the
    batch ETA is derived from measured completion throughput and a per-track
    eta_seconds deliberately has no path into it (see
    tests/test_batch_eta_model.py). These are widget tests: the footer's job is
    to render whatever the aggregator hands it, and nothing else.
    """
    a = BatchProgressAggregator(speed_smoothing=1.0)
    a.reset([str(i) for i in range(total)])
    # Drive to the requested aggregate progress with byte-weighted jobs.
    for i in range(total):
        if i < done:
            a.complete(str(i), final_bytes=1_000_000)
        elif i == done:
            a.update(str(i), downloaded_bytes=int(1_000_000 * ((progress * total) - done)),
                     total_bytes=1_000_000, speed_bps=speed)
    return dataclasses.replace(
        a.snapshot(),
        eta_seconds=eta,
        eta_lower_seconds=eta_lower,
        eta_upper_seconds=eta_upper,
        eta_confidence=eta_confidence,
    )


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

    def test_counter_counts_settled_work_not_just_successes(self, bar):
        """A cancelled (or failed) track is finished with. Counting only
        successes left the footer reading "2 of 3" with nothing left to
        download — a counter that never reaches its own total."""
        from ui import i18n

        agg = BatchProgressAggregator(speed_smoothing=1.0)
        agg.reset(["a", "b", "c"])
        agg.complete("a", final_bytes=1_000_000)
        agg.complete("b", final_bytes=1_000_000)
        agg.cancel("c")

        original = i18n._current
        try:
            i18n.set_language("en")
            bar.show_batch_progress(agg.snapshot())
        finally:
            i18n.set_language(original)

        assert "3 of 3" in bar._status_lbl.text(), bar._status_lbl.text()


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


# ── Live ETA countdown (seconds-granular, ticks between snapshots) ──────────

class TestEtaCountdown:
    """The footer ETA used to round to whole minutes above 60s and only
    repaint when a batch snapshot arrived — and snapshots stop entirely
    between tracks, so the number sat frozen. It now renders M:SS / H:MM:SS
    and ticks itself down at 1 Hz between snapshots."""

    def test_eta_is_seconds_granular(self, bar):
        bar.show_batch_progress(_snapshot(eta=452.0))
        text = bar._eta_lbl.text()
        # "7:32", not "About 8 min left".
        assert "7:32" in text
        assert "min" not in text

    def test_eta_renders_hours_when_long(self, bar):
        bar.show_batch_progress(_snapshot(eta=3725.0))
        assert "1:02:05" in bar._eta_lbl.text()

    def test_eta_renders_one_countdown_not_uncertainty_range(self, bar):
        bar.show_batch_progress(_snapshot(
            eta=452.0,
            eta_lower=390.2,
            eta_upper=540.1,
            eta_confidence="low",
        ))
        assert "7:32" in bar._eta_lbl.text()
        assert "6:30" not in bar._eta_lbl.text()
        assert "9:01" not in bar._eta_lbl.text()

    def test_small_batch_uses_the_same_single_countdown_wording(self, bar):
        bar.show_batch_progress(_snapshot(
            eta=42.0,
            eta_lower=None,
            eta_upper=None,
            eta_confidence="current_speed",
        ))
        assert "0:42" in bar._eta_lbl.text()
        assert "About" not in bar._eta_lbl.text()

    def test_hebrew_eta_is_one_plain_remaining_value(self, bar):
        from ui import i18n
        original = i18n.current_language()
        try:
            i18n.set_language("he")
            bar.show_batch_progress(_snapshot(
                eta=87.0, eta_lower=82.0, eta_upper=92.0,
                eta_confidence="low",
            ))
            text = bar._eta_lbl.text()
            assert "נותרו" in text
            assert "1:27" in text
            assert "בערך" not in text
            assert "1:22" not in text
            assert "1:32" not in text
        finally:
            i18n.set_language(original)

    def test_calculating_placeholder_when_estimate_unavailable(self, bar):
        from ui.i18n import t
        bar.show_batch_progress(_snapshot(eta=None))
        assert bar._eta_lbl.text() == t("eta_calculating")
        assert not bar._eta_timer.isActive()

    def test_countdown_timer_runs_during_a_download(self, bar):
        bar.show_batch_progress(_snapshot(eta=300.0))
        assert bar._eta_timer.isActive()

    def test_tick_decrements_the_displayed_value(self, bar):
        import time as _time
        bar.show_batch_progress(_snapshot(eta=300.0))
        assert "5:00" in bar._eta_lbl.text()
        # Pretend two seconds have passed since the snapshot, then tick.
        bar._eta_base_at = _time.monotonic() - 2.0
        bar._tick_eta()
        assert "4:58" in bar._eta_lbl.text()

    def test_tick_floors_at_zero_and_never_goes_negative(self, bar):
        import time as _time
        bar.show_batch_progress(_snapshot(eta=3.0))
        bar._eta_base_at = _time.monotonic() - 120.0
        bar._tick_eta()
        assert "0:00" in bar._eta_lbl.text()
        assert "-" not in bar._eta_lbl.text()

    def test_a_new_snapshot_re_anchors_the_countdown(self, bar):
        import time as _time
        bar.show_batch_progress(_snapshot(eta=300.0))
        bar._eta_base_at = _time.monotonic() - 30.0
        bar._tick_eta()
        assert "4:30" in bar._eta_lbl.text()
        # The aggregator is authoritative; the ticker only fills the gaps.
        bar.show_batch_progress(_snapshot(eta=600.0))
        assert "10:00" in bar._eta_lbl.text()

    def test_countdown_stops_when_paused(self, bar):
        bar.show_batch_progress(_snapshot(eta=300.0))
        assert bar._eta_timer.isActive()
        bar.show_paused()
        assert not bar._eta_timer.isActive()

    def test_countdown_stops_when_cancelling(self, bar):
        bar.show_batch_progress(_snapshot(eta=300.0))
        bar.show_cancelling()
        assert not bar._eta_timer.isActive()

    def test_countdown_stops_when_idle(self, bar):
        bar.show_batch_progress(_snapshot(eta=300.0))
        bar.reset_to_idle()
        assert not bar._eta_timer.isActive()
        assert bar._eta_lbl.text() == ""

    def test_tick_outside_downloading_is_inert(self, bar):
        bar.show_batch_progress(_snapshot(eta=300.0))
        bar.show_paused()
        before = bar._eta_lbl.text()
        bar._tick_eta()
        assert bar._eta_lbl.text() == before


class TestEtaLabelFitsBothLocales:
    """The ETA slot is fixed-width so a changing duration never shifts the rest
    of the footer. It was 120px, which silently clipped even the old
    "Calculating time remaining…" placeholder. Pin the real font metrics."""

    LONGEST_SECONDS = [59, 599, 3599, 35999, 359999]   # up to 99:59:59

    def test_every_eta_string_fits_in_both_languages(self, bar):
        from ui import i18n
        from ui.direction import isolate_number
        from utils.time_format import seconds_to_str

        fm = bar._eta_lbl.fontMetrics()
        width = bar._eta_lbl.width()
        original = i18n._current
        try:
            for lang in ("en", "he"):
                i18n.set_language(lang)
                candidates = [i18n.t("eta_calculating")]
                candidates += [
                    i18n.t("eta_about_left",
                           time=isolate_number(seconds_to_str(s)))
                    for s in self.LONGEST_SECONDS
                ]
                candidates += [
                    i18n.t(
                        "eta_range_left",
                        low=isolate_number(seconds_to_str(s)),
                        high=isolate_number(seconds_to_str(s)),
                    )
                    for s in self.LONGEST_SECONDS
                ]
                candidates += [
                    i18n.t("eta_at_least_left",
                           time=isolate_number(seconds_to_str(s)))
                    for s in self.LONGEST_SECONDS
                ]
                for text in candidates:
                    assert fm.horizontalAdvance(text) <= width, (
                        f"{lang}: {text!r} needs "
                        f"{fm.horizontalAdvance(text)}px but the slot is {width}px"
                    )
        finally:
            i18n.set_language(original)


class TestEtaNeverFreezesOnScreen:
    """End to end: a real aggregator on a fake clock, its snapshots pushed into
    a real StatusBar at the heartbeat rate, through a quiet period.

    This is the interaction that made the bug visible rather than merely
    theoretical. The aggregator's estimate used to be provably constant between
    one and three measured cycles, and because the footer re-anchors its local
    countdown on every snapshot, the heartbeat re-publishing that identical
    value twice a second held the 1 Hz ticker permanently at its starting
    point. Neither piece looks wrong alone.
    """

    CYCLE = 10.0

    class _Clock:
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            return self.t

        def advance(self, dt):
            self.t += dt

    def _warm_aggregator(self, total=20, completions=4):
        clock = self._Clock()
        a = BatchProgressAggregator(speed_smoothing=1.0, time_fn=clock)
        a.reset([f"k{i}" for i in range(total)])
        for i in range(completions):
            clock.advance(self.CYCLE)
            a.complete(f"k{i}")
        return a, clock

    def _rendered_over_quiet_period(self, bar, quiet_s, from_stall=0.0):
        a, clock = self._warm_aggregator()
        if from_stall:
            clock.advance(from_stall)
        seen = []
        ticks = int(quiet_s / 0.5)
        for _ in range(ticks):
            clock.advance(0.5)                    # one heartbeat
            bar.show_batch_progress(a.snapshot())
            seen.append(bar._eta_lbl.text())
        return seen

    def test_display_changes_around_one_and_a_half_cycles(self, bar):
        seen = self._rendered_over_quiet_period(bar, quiet_s=4.0, from_stall=13.0)
        assert len(set(seen)) > 1, f"footer stuck on {seen[0]!r}"

    def test_display_changes_around_two_and_a_half_cycles(self, bar):
        seen = self._rendered_over_quiet_period(bar, quiet_s=4.0, from_stall=23.0)
        assert len(set(seen)) > 1, f"footer stuck on {seen[0]!r}"

    def test_display_never_stalls_across_the_whole_quiet_period(self, bar):
        """Sample right through the old dead zone (1x to 3x the cycle) and
        require the rendered string to keep moving."""
        seen = self._rendered_over_quiet_period(bar, quiet_s=30.0)
        longest = best = 1
        for i in range(1, len(seen)):
            longest = longest + 1 if seen[i] == seen[i - 1] else 1
            best = max(best, longest)
        # At 2 Hz against a 1s-granular display, two identical readings in a row
        # are expected; a long run of them is the bug.
        assert best <= 3, f"footer held the same text {best} heartbeats running: {seen}"

    def test_healthy_countdown_still_decreases(self, bar):
        """The fix must not turn a normal cycle into a rising number."""
        a, clock = self._warm_aggregator()
        first = None
        last = None
        for _ in range(10):                       # 5s, well inside one cycle
            clock.advance(0.5)
            bar.show_batch_progress(a.snapshot())
            last = bar._eta_base_seconds
            if first is None:
                first = last
        assert last < first
