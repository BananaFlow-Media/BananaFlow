"""
tests/test_yt_dlp_opts.py  –  JS runtime selection for yt-dlp options
========================================================================
"""

from __future__ import annotations

from utils import yt_dlp_opts


class TestJsRuntimePreference:

    def test_preference_order_is_deno_node_quickjs(self):
        assert yt_dlp_opts._JS_RUNTIMES_PREFERENCE == ("deno", "node", "quickjs")

    def test_bun_is_not_a_default_candidate(self):
        assert "bun" not in yt_dlp_opts._JS_RUNTIMES_PREFERENCE


class TestNodeMajorVersion:

    def test_parses_standard_output(self):
        assert yt_dlp_opts._node_major_version("v22.11.0\n") == 22

    def test_parses_without_v_prefix(self):
        assert yt_dlp_opts._node_major_version("24.1.0") == 24

    def test_empty_output_returns_none(self):
        assert yt_dlp_opts._node_major_version("") is None

    def test_garbage_output_returns_none(self):
        assert yt_dlp_opts._node_major_version("not a version") is None


class TestDetectJsRuntimes:
    """Exercise _detect_js_runtimes() with PATH lookups and node-version
    checks stubbed out, so the test doesn't depend on what's actually
    installed on the machine running the suite."""

    def _which_only(self, monkeypatch, available: dict[str, str]):
        def fake_which(name):
            return available.get(name)
        monkeypatch.setattr(yt_dlp_opts.shutil, "which", fake_which)

    def test_deno_preferred_when_present(self, monkeypatch):
        self._which_only(monkeypatch, {"deno": "/usr/bin/deno", "node": "/usr/bin/node"})
        monkeypatch.setattr(yt_dlp_opts, "_get_node_version_output", lambda p: "v22.0.0")
        assert yt_dlp_opts._detect_js_runtimes() == {"deno": {"path": "/usr/bin/deno"}}

    def test_node_used_when_version_22_or_above(self, monkeypatch):
        self._which_only(monkeypatch, {"node": "/usr/bin/node"})
        monkeypatch.setattr(yt_dlp_opts, "_get_node_version_output", lambda p: "v22.11.0")
        assert yt_dlp_opts._detect_js_runtimes() == {"node": {"path": "/usr/bin/node"}}

    def test_node_rejected_when_below_22(self, monkeypatch):
        self._which_only(monkeypatch, {"node": "/usr/bin/node"})
        monkeypatch.setattr(yt_dlp_opts, "_get_node_version_output", lambda p: "v20.18.0")
        # Node is too old and nothing else is available -> fallback default,
        # never {"node": ...}.
        result = yt_dlp_opts._detect_js_runtimes()
        assert "node" not in result
        assert result == {"deno": {}}

    def test_node_rejected_falls_back_to_quickjs(self, monkeypatch):
        self._which_only(monkeypatch, {"node": "/usr/bin/node", "qjs": "/usr/bin/qjs"})
        monkeypatch.setattr(yt_dlp_opts, "_get_node_version_output", lambda p: "v18.0.0")
        assert yt_dlp_opts._detect_js_runtimes() == {"quickjs": {"path": "/usr/bin/qjs"}}

    def test_quickjs_used_when_nothing_else_available(self, monkeypatch):
        self._which_only(monkeypatch, {"qjs": "/usr/bin/qjs"})
        assert yt_dlp_opts._detect_js_runtimes() == {"quickjs": {"path": "/usr/bin/qjs"}}

    def test_bun_never_auto_selected(self, monkeypatch):
        # Even if bun is the only runtime on PATH, it must not be picked —
        # it is intentionally excluded from the preference list.
        self._which_only(monkeypatch, {"bun": "/usr/bin/bun"})
        result = yt_dlp_opts._detect_js_runtimes()
        assert "bun" not in result
        assert result == {"deno": {}}

    def test_nothing_available_falls_back_to_deno_default(self, monkeypatch):
        self._which_only(monkeypatch, {})
        assert yt_dlp_opts._detect_js_runtimes() == {"deno": {}}


class TestBundledPotProviderArgs:

    def test_base_opts_include_bundled_provider_extractor_args(self, monkeypatch):
        expected = {
            "youtubepot-bgutilscript": {
                "server_home": ["C:/BananaFlow/pot-provider-backend/bgutil-ytdlp-pot-provider/server"],
            },
        }
        monkeypatch.setattr(yt_dlp_opts, "_detect_js_runtimes", lambda: {"deno": {"path": "deno.exe"}})
        monkeypatch.setattr(yt_dlp_opts, "_detect_bundled_pot_provider_args", lambda: expected)

        opts = yt_dlp_opts.build_base_ydl_opts()

        assert opts["extractor_args"] == expected

    def test_base_opts_omit_extractor_args_when_no_backend(self, monkeypatch):
        monkeypatch.setattr(yt_dlp_opts, "_detect_bundled_pot_provider_args", lambda: {})

        opts = yt_dlp_opts.build_base_ydl_opts()

        assert "extractor_args" not in opts

    def test_metadata_search_can_explicitly_skip_the_provider(self, monkeypatch):
        monkeypatch.setattr(
            yt_dlp_opts,
            "_detect_bundled_pot_provider_args",
            lambda: (_ for _ in ()).throw(AssertionError("provider must not be probed")),
        )

        opts = yt_dlp_opts.build_base_ydl_opts(enable_po_token_provider=False)

        assert "extractor_args" not in opts


class TestPoTokenCircuitBreaker:

    def setup_method(self):
        yt_dlp_opts.reset_po_token_provider_circuit()

    def teardown_method(self):
        yt_dlp_opts.reset_po_token_provider_circuit()

    def test_opens_after_two_provider_failures(self):
        assert not yt_dlp_opts.note_po_token_provider_attempt_failure()
        assert not yt_dlp_opts.po_token_provider_circuit_open()
        assert yt_dlp_opts.note_po_token_provider_attempt_failure()
        assert yt_dlp_opts.po_token_provider_circuit_open()

    def test_ignores_unrelated_ytdlp_errors(self):
        assert not yt_dlp_opts.note_po_token_provider_failure("HTTP Error 403")
        assert not yt_dlp_opts.po_token_provider_circuit_open()

    def test_open_circuit_omits_provider_configuration(self, monkeypatch):
        expected = {"youtubepot-bgutilscript": {"server_home": ["C:/provider"]}}
        monkeypatch.setattr(yt_dlp_opts, "_detect_bundled_pot_provider_args", lambda: expected)
        yt_dlp_opts.note_po_token_provider_attempt_failure()
        yt_dlp_opts.note_po_token_provider_attempt_failure()

        opts = yt_dlp_opts.build_base_ydl_opts()

        assert "extractor_args" not in opts

    def test_matcher_can_preserve_provider_after_download_circuit_opens(self, monkeypatch):
        expected = {"youtubepot-bgutilscript": {"server_home": ["C:/provider"]}}
        monkeypatch.setattr(yt_dlp_opts, "_detect_bundled_pot_provider_args", lambda: expected)
        monkeypatch.setattr(yt_dlp_opts, "install_bgutil_stderr_capture", lambda: None)
        yt_dlp_opts.note_po_token_provider_attempt_failure()
        yt_dlp_opts.note_po_token_provider_attempt_failure()

        opts = yt_dlp_opts.build_base_ydl_opts(respect_po_token_circuit=False)

        assert opts["extractor_args"] == expected

    def test_bgutil_stderr_is_captured_without_affecting_other_commands(self, monkeypatch):
        from yt_dlp.utils import Popen

        calls = []

        def fake_run(command, *args, **kwargs):
            calls.append((command, kwargs))
            return "stdout", "Failed while generating POT", 1

        monkeypatch.setattr(Popen, "run", staticmethod(fake_run))
        monkeypatch.setattr(yt_dlp_opts, "_bgutil_stderr_capture_installed", False)
        yt_dlp_opts.install_bgutil_stderr_capture()

        Popen.run(["deno", "run", "generate_once.ts"])
        Popen.run(["deno", "run", "generate_once.ts"])
        Popen.run(["ffmpeg", "-version"])

        assert calls[0][1]["stderr"] is yt_dlp_opts.subprocess.PIPE
        assert calls[1][1]["stderr"] is yt_dlp_opts.subprocess.PIPE
        assert "stderr" not in calls[2][1]
        assert yt_dlp_opts.po_token_provider_circuit_open()

    def test_diagnostics_do_not_spend_provider_attempt_budget(self):
        yt_dlp_opts.note_po_token_provider_diagnostic("po_token_missing")
        yt_dlp_opts.note_po_token_provider_diagnostic("PoTokenProviderError")

        metrics = yt_dlp_opts.po_token_provider_metrics()
        assert metrics["diagnostics"] == 2
        assert metrics["attempts"] == 0
        assert not metrics["circuit_open"]

    def test_process_breaker_survives_a_new_orchestrator_batch(self, tmp_path):
        from core.download_orchestrator import DownloadOrchestrator
        from core.downloader import DownloadRequest, MediaType

        class Engine:
            def __init__(self):
                import threading
                self._cancel_event = threading.Event()

            def cancel_all(self):
                self._cancel_event.set()

            def download(self, req):
                req.on_finished(type("P", (), {
                    "total_bytes": None, "downloaded_bytes": None,
                    "total_bytes_estimate": None, "output_path": "",
                    "warning_message": "",
                })())

        class Callbacks:
            def __getattr__(self, _name):
                return lambda *args, **kwargs: None

        yt_dlp_opts.note_po_token_provider_attempt_failure()
        req = DownloadRequest(url="https://example.com/a", output_dir=str(tmp_path), media_type=MediaType.AUDIO)
        DownloadOrchestrator(Engine(), Callbacks(), max_workers=1).run_batch([("k", req)])

        assert yt_dlp_opts.note_po_token_provider_attempt_failure()
        assert yt_dlp_opts.po_token_provider_circuit_open()
