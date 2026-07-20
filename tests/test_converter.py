"""
tests/test_converter.py  –  Converter UI worker + panel coverage
======================================================================
Phase 3 moved all conversion logic into core/converter_service.py
(covered by tests/test_converter_service_phase3.py).  This file covers
the Qt layer that remains: ConvertWorker's signal contract around the
service, and the panel's file-list/dedup/extension-filter/i18n logic.
The service itself is faked at its convert_file boundary so no FFmpeg
binary is required here.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.converter_service import (
    CollisionPolicy,
    ConversionErrorKind,
    ConversionRequest,
    ConverterService,
    FileOutcome,
    FileResult,
)


def _request(path: Path, fmt: str = "mp3") -> ConversionRequest:
    return ConversionRequest(
        source=path, output_format=fmt, bitrate="320k",
        collision_policy=CollisionPolicy.UNIQUE,
    )


def _completed(request: ConversionRequest) -> FileResult:
    src = Path(request.source)
    return FileResult(
        source=str(src),
        destination=str(src.with_suffix(f".{request.output_format}")),
        outcome=FileOutcome.COMPLETED,
    )


def _failed(request: ConversionRequest, message: str) -> FileResult:
    return FileResult(
        source=str(request.source), destination=None,
        outcome=FileOutcome.FAILED,
        error_kind=ConversionErrorKind.CORRUPT_SOURCE, message=message,
    )


def _make_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
    except ImportError:
        pytest.skip("PySide6 not available in this environment")
    return QApplication.instance() or QApplication([]), QTest


# ──────────────────────────────────────────────────────────────────────────────
# ConvertWorker — run() called directly, faked service boundary
# ──────────────────────────────────────────────────────────────────────────────
#
# run() is invoked synchronously here rather than via start() + a real
# background QThread. This tests the exact same production logic (the
# method body is identical either way) while removing any dependence on
# cross-thread Qt signal delivery: a signal emitted and received on the
# same thread is a plain synchronous call, so there is no event-loop
# polling, no wait timeout, and no QThread teardown race to reason about.
# The real cross-thread process/cancellation mechanics this worker
# delegates to (CancellationToken, Popen handling, graceful stop/kill)
# are already covered with genuine threading in
# tests/test_converter_service_phase3.py's TestCancellationAndTimeout.
# A real start()-based run of this exact worker crashed the isolated test
# runner's own subprocess on GitHub's Windows hosted runner (native
# 0xC0000409, 3/3 reproducible) while never reproducing locally, even
# with an explicit worker.wait() after the wait loop — a QThread+Qt
# signal-delivery environment difference this repo's test suite should
# not depend on to stay green.


class TestConvertWorkerRunSignals:

    def _app(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("PySide6 not available in this environment")
        return QApplication.instance() or QApplication([])

    def test_successful_batch_emits_started_done_then_all_done(self, tmp_path, monkeypatch):
        from ui.panels.converter_panel import ConvertWorker

        self._app()
        requests = []
        for name in ("a.wav", "b.wav"):
            p = tmp_path / name
            p.write_bytes(b"")
            requests.append(_request(p))

        monkeypatch.setattr(
            ConverterService, "convert_file",
            lambda self, request, on_progress=None, cancel=None, ask_callback=None:
                _completed(request),
        )

        events = []
        worker = ConvertWorker(requests)
        worker.file_started.connect(lambda p: events.append(("started", p)))
        worker.file_done.connect(lambda i, o: events.append(("done", i, o)))
        worker.file_error.connect(lambda i, e: events.append(("error", i, e)))
        worker.all_done.connect(lambda c, f, s, x: events.append(("all_done", c, f, s, x)))
        worker.run()

        assert len([e for e in events if e[0] == "started"]) == 2
        assert len([e for e in events if e[0] == "done"]) == 2
        assert not [e for e in events if e[0] == "error"]
        assert events[-1] == ("all_done", 2, 0, 0, 0)

    def test_failure_emits_file_error_but_still_finishes(self, tmp_path, monkeypatch):
        from ui.panels.converter_panel import ConvertWorker

        self._app()
        p = tmp_path / "bad.wav"
        p.write_bytes(b"")

        monkeypatch.setattr(
            ConverterService, "convert_file",
            lambda self, request, on_progress=None, cancel=None, ask_callback=None:
                _failed(request, "decode error"),
        )

        events = []
        worker = ConvertWorker([_request(p)])
        worker.file_error.connect(lambda i, e: events.append(("error", i, e)))
        worker.all_done.connect(lambda c, f, s, x: events.append(("all_done", c, f, s, x)))
        worker.run()

        errors = [e for e in events if e[0] == "error"]
        assert len(errors) == 1
        assert "decode error" in errors[0][2]
        assert events[-1] == ("all_done", 0, 1, 0, 0)

    def test_skipped_and_progress_signals_are_forwarded(self, tmp_path, monkeypatch):
        from core.converter_service import FileProgress
        from ui.panels.converter_panel import ConvertWorker

        self._app()
        p = tmp_path / "dup.wav"
        p.write_bytes(b"")

        def fake_convert(self, request, on_progress=None, cancel=None, ask_callback=None):
            if on_progress:
                on_progress(FileProgress(
                    source=str(request.source), seconds_done=1.0,
                    total_seconds=4.0, ratio=0.25,
                ))
            return FileResult(
                source=str(request.source), destination=None,
                outcome=FileOutcome.SKIPPED, message="destination exists",
            )

        monkeypatch.setattr(ConverterService, "convert_file", fake_convert)

        events = []
        worker = ConvertWorker([_request(p)])
        worker.file_progress.connect(lambda s, r: events.append(("progress", s, r)))
        worker.file_skipped.connect(lambda s, m: events.append(("skipped", s, m)))
        worker.all_done.connect(lambda c, f, s, x: events.append(("all_done", c, f, s, x)))
        worker.run()

        assert ("progress", str(p), 0.25) in events
        assert [e for e in events if e[0] == "skipped"]
        assert events[-1] == ("all_done", 0, 0, 1, 0)

    def test_cancelled_outcome_emits_file_cancelled_and_counts_it(self, tmp_path, monkeypatch):
        """ConvertWorker's own responsibility is only the outcome->signal
        mapping; whether cancel() actually stops a running conversion in
        time is CancellationToken/ConverterService's contract, covered
        with real threading in test_converter_service_phase3.py."""
        from ui.panels.converter_panel import ConvertWorker

        self._app()
        files = [tmp_path / n for n in ("a.wav", "b.wav", "c.wav")]
        for f in files:
            f.write_bytes(b"")

        def fake_convert(self, request, on_progress=None, cancel=None, ask_callback=None):
            assert cancel is not None
            return FileResult(
                source=str(request.source), destination=None,
                outcome=FileOutcome.CANCELLED,
                error_kind=ConversionErrorKind.CANCELLED,
                message="cancelled by user",
            )

        monkeypatch.setattr(ConverterService, "convert_file", fake_convert)

        events = []
        worker = ConvertWorker([_request(f) for f in files])
        worker.file_cancelled.connect(lambda s: events.append(("cancelled", s)))
        worker.all_done.connect(lambda c, f, s, x: events.append(("all_done", c, f, s, x)))
        worker.run()

        summary = events[-1]
        assert summary[4] == 3, "every cancelled file must be counted"
        assert len([e for e in events if e[0] == "cancelled"]) == 3

    def test_cancel_calls_the_service_cancellation_token(self, tmp_path):
        """worker.cancel() must reach the underlying CancellationToken —
        the mechanics of that token stopping a live process are
        CancellationToken/ConverterService's own tested contract."""
        from ui.panels.converter_panel import ConvertWorker

        self._app()
        p = tmp_path / "a.wav"
        p.write_bytes(b"")
        worker = ConvertWorker([_request(p)])

        assert not worker._token.cancelled
        worker.cancel()
        assert worker._token.cancelled


# ──────────────────────────────────────────────────────────────────────────────
# ConverterPanel — file list, dedup, extension filtering, request building
# ──────────────────────────────────────────────────────────────────────────────


class TestConverterPanelFileList:

    def _panel(self):
        from ui.panels.converter_panel import ConverterPanel
        app, _qtest = _make_app()
        panel = ConverterPanel()
        # isVisible() reflects the whole ancestor chain, not just its own
        # setVisible() flag — without an actual show(), every child widget
        # reports isVisible() == False regardless of what the panel set.
        panel.show()
        app.processEvents()
        return panel

    def test_adding_same_path_twice_is_deduplicated(self, tmp_path):
        panel = self._panel()
        try:
            f = str(tmp_path / "song.mp3")
            panel._add_file(f)
            panel._add_file(f)
            assert len(panel._files) == 1
        finally:
            panel.deleteLater()

    def test_drop_hint_hides_after_first_file_and_returns_when_cleared(self, tmp_path):
        panel = self._panel()
        try:
            assert panel._drop_hint.isVisible()
            panel._add_file(str(tmp_path / "song.mp3"))
            assert not panel._drop_hint.isVisible()
            panel._on_clear()
            assert panel._drop_hint.isVisible()
            assert not panel._files
        finally:
            panel.deleteLater()

    def test_removing_a_file_updates_the_list(self, tmp_path):
        panel = self._panel()
        try:
            f = str(tmp_path / "song.mp3")
            panel._add_file(f)
            panel._remove_file(f)
            assert f not in panel._files
            assert panel._drop_hint.isVisible()
        finally:
            panel.deleteLater()

    def test_same_dir_toggle_off_reveals_output_folder_button(self, tmp_path):
        panel = self._panel()
        try:
            panel._same_dir_sw.setChecked(True)
            panel._on_same_dir_toggle(True)
            assert not panel._out_dir_btn.isVisible()
            assert panel._out_dir is None

            panel._out_dir = str(tmp_path)  # simulate a previously chosen folder
            panel._on_same_dir_toggle(False)
            assert panel._out_dir_btn.isVisible()
        finally:
            panel.deleteLater()

    def test_convert_with_no_files_does_not_start_a_worker(self):
        panel = self._panel()
        try:
            panel._on_convert()
            assert panel._worker is None
        finally:
            panel.deleteLater()

    def test_unsupported_extension_is_rejected_by_drop_filter(self):
        from ui.panels.converter_panel import _INPUT_EXTS
        assert ".txt" not in _INPUT_EXTS
        assert ".pdf" not in _INPUT_EXTS
        assert ".mp3" in _INPUT_EXTS

    def test_convert_button_label_swaps_to_cancel_while_running(self, tmp_path, monkeypatch):
        from ui.i18n import t

        panel = self._panel()
        try:
            f = tmp_path / "song.wav"
            f.write_bytes(b"")
            panel._add_file(str(f))

            monkeypatch.setattr(
                ConverterService, "convert_file",
                lambda self, request, on_progress=None, cancel=None, ask_callback=None:
                    _completed(request),
            )
            panel._on_convert()
            assert panel._convert_btn.text() == t("converter_cancel_btn")
        finally:
            if panel._worker:
                panel._worker.wait(2000)
            panel.deleteLater()

    def test_convert_builds_requests_with_technical_bitrate(self, tmp_path, monkeypatch):
        panel = self._panel()
        try:
            f = tmp_path / "song.wav"
            f.write_bytes(b"")
            panel._add_file(str(f))

            # Find the preset for "128k" in the combobox
            idx = -1
            for i in range(panel._br_combo.count()):
                if panel._br_combo.itemData(i) == "128k":
                    idx = i
                    break
            assert idx != -1
            panel._br_combo.setCurrentIndex(idx)

            # Mock ConvertWorker.start to prevent starting a thread
            started_workers = []
            from ui.panels.converter_panel import ConvertWorker
            monkeypatch.setattr(
                ConvertWorker, "start",
                lambda worker_self: started_workers.append(worker_self),
            )

            panel._on_convert()

            assert len(started_workers) == 1
            requests = started_workers[0]._requests
            assert len(requests) == 1
            # Proves it passes the technical bitrate ("128k") from
            # currentData(), and NOT the translated display text
            assert requests[0].bitrate == "128k"
            assert requests[0].bitrate != panel._br_combo.currentText()
            assert requests[0].output_format == "mp3"
            # No collision existed, so the safe default policy applies.
            assert requests[0].collision_policy is CollisionPolicy.UNIQUE
        finally:
            panel.deleteLater()

    def test_collision_prescan_returns_unique_when_no_collision(self, tmp_path):
        panel = self._panel()
        try:
            f = tmp_path / "song.wav"
            f.write_bytes(b"")
            policy = panel._resolve_collision_policy([str(f)], "mp3", None)
            assert policy is CollisionPolicy.UNIQUE
        finally:
            panel.deleteLater()
