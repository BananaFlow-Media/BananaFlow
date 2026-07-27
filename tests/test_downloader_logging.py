"""
tests/test_downloader_logging.py  –  SilentLogger warning visibility
========================================================================
Regression guard for reliability-hardening phase 1: SilentLogger must
surface (not swallow) the yt-dlp messages that explain why a download is
about to fail, and must tag them with their warning_classifier category.
Purely routine per-client fallback chatter should still be filtered.
"""

from __future__ import annotations

import logging

import pytest

from core.downloader import SilentLogger


CRITICAL_MESSAGES = [
    ("WARNING: YouTube account cookies are no longer valid", "cookies_expired_or_invalid"),
    ("ERROR: No supported JavaScript runtime could be found", "js_runtime_missing"),
    ("HTTP Error 403: Forbidden", "rate_limited_or_forbidden"),
    ("ERROR: Sign in to confirm you're not a bot", "rate_limited_or_forbidden"),
]

STILL_FILTERED_NOISE = [
    "Signature solving failed",
    "n challenge solving failed",
    "Incomplete data received",
    "Some formats may be missing",
    "unable to extract yt initial data",
]


class TestSilentLoggerCriticalWarnings:

    @pytest.fixture(autouse=True)
    def _reset_provider_telemetry(self):
        from utils.yt_dlp_opts import reset_po_token_provider_circuit
        reset_po_token_provider_circuit()
        yield
        reset_po_token_provider_circuit()

    def test_repeated_provider_failures_open_the_circuit(self):
        from utils.yt_dlp_opts import (
            note_po_token_provider_attempt_failure,
            po_token_provider_circuit_open,
        )

        logger = SilentLogger()
        logger.warning("Failed while generating POT")
        logger.error("PoTokenProviderError")
        assert not po_token_provider_circuit_open()
        note_po_token_provider_attempt_failure()
        assert not po_token_provider_circuit_open()
        note_po_token_provider_attempt_failure()
        assert po_token_provider_circuit_open()

    def test_repeated_provider_messages_are_coalesced(self, caplog):
        caplog.set_level(logging.WARNING, logger="core.downloader")
        logger = SilentLogger()
        logger.warning("WARNING: Unable to fetch GVS PO Token")
        logger.warning("WARNING: Unable to fetch GVS PO Token")
        logger.error("PoTokenProviderError")

        assert caplog.text.count("provider-related warning observed") == 1
        assert "PoTokenProviderError" not in caplog.text

    @pytest.mark.parametrize("message, category", CRITICAL_MESSAGES)
    def test_critical_warning_not_suppressed(self, caplog, message, category):
        caplog.set_level(logging.WARNING, logger="core.downloader")
        SilentLogger().warning(message)
        assert message in caplog.text
        assert category in caplog.text

    def test_critical_error_not_suppressed(self, caplog):
        caplog.set_level(logging.ERROR, logger="core.downloader")
        SilentLogger().error("WARNING: Unable to fetch GVS PO Token")
        assert "Unable to fetch GVS PO Token" in caplog.text
        assert "provider-related error observed" in caplog.text

    @pytest.mark.parametrize("message", STILL_FILTERED_NOISE)
    def test_routine_noise_still_filtered(self, caplog, message):
        caplog.set_level(logging.WARNING, logger="core.downloader")
        SilentLogger().warning(message)
        assert caplog.text == ""

    def test_unclassified_warning_still_logged_without_tag(self, caplog):
        caplog.set_level(logging.WARNING, logger="core.downloader")
        SilentLogger().warning("Some unrelated informational warning")
        assert "Some unrelated informational warning" in caplog.text
