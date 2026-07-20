"""
core/youtube_doctor.py  –  YouTube reliability diagnostics ("YouTube Doctor")
================================================================================
Reliability-hardening phase 3: a diagnostic layer that answers one
question — "is this system set up for reliable YouTube downloads?" —
without changing any downloader behavior.

This module only *detects and reports*. It does not:
  * generate, scrape, store, or inject PO Tokens itself,
  * implement a custom PO Token fallback, or change yt-dlp provider
    configuration at check time (packaged builds configure yt-dlp through
    utils.yt_dlp_opts using the provider's official server_home option),
  * add retries or bypass logic,
  * make network calls (everything here is local/offline).

Checks
------
  * yt_dlp_version          — installed yt-dlp vs. the known minimum.
  * yt_dlp_ejs               — is the EJS player-logic plugin importable.
  * js_runtime               — Deno / Node 22+ / QuickJS availability,
                                mirroring utils.yt_dlp_opts's own selection
                                logic (Bun is never selected).
  * cookies                  — file/browser mode, presence, and whether
                                YouTube/login-looking cookies are present.
                                Never reads or exposes cookie *values*, and
                                never claims cookies are definitely valid.
  * po_token_provider         — plugin/backend/runtime readiness.
  * youtube_reliability_mode  — echoes the app's current conservative/fast
                                 setting and what it means.

Design
------
* Zero GUI imports – pure stdlib (+ reuse of utils.yt_dlp_opts's runtime
  detection, which is itself GUI-free).
* Every check returns a DoctorCheck: a machine-readable ``status`` plus a
  separate human-readable ``message`` — callers should never need to
  parse the message to make a decision.
* Environment-probing checks accept injectable callables (defaulting to
  the real stdlib functions) so tests can simulate "no JS runtime",
  "no PO Token Provider", etc. without touching the actual machine.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import pkgutil
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Status + result types
# ──────────────────────────────────────────────────────────────────────────────

class DoctorStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


_SEVERITY_ORDER = {DoctorStatus.PASS: 0, DoctorStatus.WARN: 1, DoctorStatus.FAIL: 2}
_STATUS_ICON = {DoctorStatus.PASS: "✅", DoctorStatus.WARN: "⚠", DoctorStatus.FAIL: "❌"}


@dataclass
class DoctorCheck:
    """One diagnostic result. ``status`` is machine-readable; ``message``
    is the friendly explanation — kept as separate fields on purpose.

    ``message``/``detail`` are always canonical English (this module has
    zero GUI imports and cannot know the UI language). ``message_key`` /
    ``detail_key`` + their params let the UI layer re-render the same
    content in the user's language via ui.i18n — the English templates
    in DOCTOR_TEXTS_EN are injected into TRANSLATIONS["en"] so the two
    can never drift.
    """
    category: str
    status:   DoctorStatus
    message:  str
    detail:   str = ""
    message_key:    str  = ""
    message_params: dict = field(default_factory=dict)
    detail_key:     str  = ""
    detail_params:  dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Canonical English texts — single source for both core output and the
# UI's "en" translation table (ui.i18n injects this dict verbatim).
# Keys are stable identifiers the UI translates with t(key, **params).
# ──────────────────────────────────────────────────────────────────────────────

DOCTOR_TEXTS_EN: dict[str, str] = {
    # yt-dlp version
    "doctor_yt_dlp_missing": "yt-dlp is not importable.",
    "doctor_yt_dlp_missing_action": "Install yt-dlp: pip install -U yt-dlp[default] ({exc})",
    "doctor_yt_dlp_ok": "yt-dlp {installed} meets the minimum required version ({minimum}).",
    "doctor_yt_dlp_outdated": (
        "yt-dlp {installed} is older than the recommended minimum "
        "({minimum}). Older releases are more likely to hit "
        "PO Token / signature-solving failures YouTube has since changed."
    ),
    "doctor_yt_dlp_outdated_action": "Update yt-dlp: pip install -U \"yt-dlp[default]>={minimum}\"",

    # yt-dlp-ejs
    "doctor_ejs_ok": "yt-dlp-ejs is installed — YouTube JS player/EJS solving is available.",
    "doctor_ejs_missing": (
        "yt-dlp-ejs is not installed. Some YouTube formats/signature "
        "solving may be unavailable without it."
    ),
    "doctor_ejs_missing_action": "Install yt-dlp-ejs: pip install -U yt-dlp[default] (bundles yt-dlp-ejs)",

    # JS runtime
    "doctor_js_ok": "Selected JS runtime: {selected} ({details}).",
    "doctor_js_ok_bundled": "Selected JS runtime: {selected} — bundled with BananaFlow ({details}).",
    "doctor_js_node_too_old": (
        "Node {version} found but requires 22+. "
        "YouTube signature/player solving may fail."
    ),
    "doctor_js_none": (
        "No supported JS runtime found on PATH. "
        "YouTube signature/player solving may fail."
    ),
    "doctor_js_action": "Install Deno (recommended), Node 22+, or QuickJS.",

    # Cookies
    "doctor_cookies_browser": (
        "Configured to use live '{browser}' browser cookies. "
        "Presence/login state can't be verified offline; yt-dlp will "
        "report an error at download time if extraction fails."
    ),
    "doctor_cookies_none": (
        "No cookies configured. Public videos will still work; "
        "age-restricted, private, or members-only videos will fail "
        "without cookies."
    ),
    "doctor_cookies_file_missing": "Configured cookies file '{name}' does not exist.",
    "doctor_cookies_file_unreadable": "Configured cookies file '{name}' could not be read.",
    "doctor_cookies_file_empty": "Cookies file '{name}' is empty. Cookies may need re-export.",
    "doctor_cookies_not_youtube": (
        "Cookies file '{name}' has entries, but none appear "
        "to be for YouTube. Cookies may need re-export."
    ),
    "doctor_cookies_no_login": (
        "YouTube cookies appear present in '{name}', but no "
        "login cookies (e.g. LOGIN_INFO/SID) were found — this may be "
        "an anonymous/consent-only session. Cookies may need re-export."
    ),
    "doctor_cookies_login_ok": (
        "Login cookies appear present in '{name}'. This does not "
        "guarantee they haven't expired — re-export if gated downloads fail."
    ),
    "doctor_cookies_reexport": "Re-export cookies from a signed-in YouTube session.",
    "doctor_cookies_permissions": "Check file permissions, or re-export cookies.",
    "doctor_cookies_reexport_on_youtube": "Re-export cookies while visiting youtube.com, signed in.",
    "doctor_cookies_reexport_signed_in": "Re-export cookies while signed in to a YouTube/Google account.",

    # PO Token Provider
    "doctor_pot_ready": (
        "PO Token Provider is ready: bgutil plugin is available, bundled "
        "Deno is selected, the Deno script backend is present, and the "
        "backend health check passed (script version {version}). yt-dlp "
        "will use the official provider mechanism with BananaFlow's bundled "
        "server_home; BananaFlow does not generate, store, or inject PO Tokens."
    ),
    "doctor_pot_plugin_only": (
        "PO Token Provider plugin is bundled, but the bundled JS runtime "
        "and provider backend are not both available. This is plugin-only "
        "staging and is not PO ready."
    ),
    "doctor_pot_plugin_runtime_no_backend": (
        "PO Token Provider plugin and JS runtime are available, but the "
        "bgutil Deno script backend is missing or incomplete. This is not "
        "PO ready until the backend/server files and node_modules are bundled."
    ),
    "doctor_pot_backend_unhealthy": (
        "PO Token Provider backend is present but failed its Deno script "
        "health check: {reason}. This is not PO ready."
    ),
    "doctor_pot_script_provider_missing": (
        "PO Token Provider backend passed its Deno script health check, but "
        "the getpot_bgutil_script provider module was not detected. This is "
        "not PO ready."
    ),
    "doctor_pot_installed_no_backend": (
        "A PO Token Provider appears installed{name_note} via {methods}. "
        "Source installs may configure a provider manually, but this BananaFlow "
        "build does not have the bundled provider backend ready."
    ),
    "doctor_pot_bundled": (
        "A PO Token Provider plugin is bundled. YouTube Doctor reports full "
        "readiness only when bundled Deno and the provider backend are present "
        "and healthy."
    ),
    "doctor_pot_installed": (
        "A PO Token Provider appears installed{name_note} via {methods}. Full "
        "readiness requires a healthy bundled provider backend."
    ),
    "doctor_pot_missing": (
        "No PO Token Provider plugin detected. Some YouTube videos may fail "
        "with a PO Token error until a provider is available for yt-dlp."
    ),
    "doctor_pot_missing_action": (
        "Update or reinstall BananaFlow so the bundled PO Token Provider files "
        "are present. For source installs, install the po-token extra and "
        "run the provider staging helper."
    ),

    # Reliability mode
    "doctor_reliability_conservative": (
        "YouTube conservative mode is active (default): multiple "
        "YouTube jobs in a batch are serialized one-at-a-time with a "
        "5-10s cooldown between them, and fragment concurrency is "
        "capped at 1. Non-YouTube downloads are unaffected."
    ),
    "doctor_reliability_fast": (
        "YouTube conservative mode is OFF ('fast' mode is an explicit "
        "opt-in). YouTube downloads run at normal parallelism/fragment "
        "concurrency, which raises the risk of 403s, rate-limiting, and "
        "PO Token/bot challenges."
    ),
}


def _mk_check(
    category: str,
    status: DoctorStatus,
    message_key: str,
    message_params: Optional[dict] = None,
    detail_key: str = "",
    detail_params: Optional[dict] = None,
) -> DoctorCheck:
    """Build a DoctorCheck whose English text is rendered from
    DOCTOR_TEXTS_EN while carrying the key+params for UI translation."""
    mp = dict(message_params or {})
    dp = dict(detail_params or {})
    return DoctorCheck(
        category=category,
        status=status,
        message=DOCTOR_TEXTS_EN[message_key].format(**mp),
        detail=DOCTOR_TEXTS_EN[detail_key].format(**dp) if detail_key else "",
        message_key=message_key,
        message_params=mp,
        detail_key=detail_key,
        detail_params=dp,
    )


@dataclass
class RuntimeStatus:
    """Per-runtime breakdown used by the js_runtime check."""
    name:      str
    found:     bool
    supported: bool = False
    version:   str  = ""


@dataclass
class CookieDiagnostics:
    """Structured cookie inspection result. Never carries a cookie value."""
    mode:                     str  = "none"   # "none" | "file" | "browser"
    file_name:                str  = ""       # basename only — never the full path
    file_exists:              bool = False
    file_readable:            bool = False
    file_non_empty:           bool = False
    has_youtube_domain_cookies: bool = False
    has_likely_login_cookies: bool = False
    browser:                  str  = ""


@dataclass
class ProviderDetection:
    """One signal suggesting a PO Token Provider plugin is installed.

    Every detection here is a *heuristic*: it relies on the (informal,
    community) "getpot_*" module-naming convention and/or a distribution
    name/RECORD matching a known hint — there is no formal registry, and
    confirming a module really is a working PO Token Provider would
    require importing/executing it, which this module deliberately never
    does.
    """
    method:            str   # "distribution" | "namespace" | "bundled" — how this was found
    heuristic:         bool = True  # always True today — name-based, not confirmed by execution
    module_name:       str  = ""    # e.g. "yt_dlp_plugins.extractor.getpot_bgutil", if known
    distribution_name: str  = ""    # installing package name, e.g. "bgutil-ytdlp-pot-provider", if known
    bundled:           bool = False  # True when shipped inside this BananaFlow build


@dataclass
class ProviderBackendDiagnostics:
    """Bundled bgutil backend readiness. Never carries a PO Token."""
    mode:              str = ""      # "script-deno" for the packaged stack
    runtime_name:      str = ""
    backend_present:   bool = False
    backend_healthy:   bool = False
    version:           str = ""
    server_home:       str = ""
    script_path:       str = ""
    reason:            str = ""


@dataclass
class YoutubeDoctorReport:
    checks:  list[DoctorCheck] = field(default_factory=list)
    cookies: CookieDiagnostics = field(default_factory=CookieDiagnostics)
    po_token_provider_detections: list[ProviderDetection] = field(default_factory=list)
    po_token_backend: ProviderBackendDiagnostics = field(default_factory=ProviderBackendDiagnostics)

    @property
    def overall_status(self) -> DoctorStatus:
        if not self.checks:
            return DoctorStatus.WARN
        return max((c.status for c in self.checks), key=lambda s: _SEVERITY_ORDER[s])

    @property
    def ready_for_public_downloads(self) -> str:
        yt_dlp_status = self._status_for("yt_dlp_version")
        runtime_status = self._status_for("js_runtime")
        if DoctorStatus.FAIL in (yt_dlp_status, runtime_status):
            return "no"
        if DoctorStatus.WARN in (yt_dlp_status, runtime_status):
            return "maybe"
        return "yes"

    @property
    def cookies_available_for_gated(self) -> str:
        if self.cookies.mode == "browser":
            return "maybe"  # configured, but presence/validity unverifiable offline
        if self.cookies.mode != "file":
            return "no"
        if self.cookies.has_likely_login_cookies:
            return "yes"
        if self.cookies.has_youtube_domain_cookies:
            return "maybe"
        return "no"

    @property
    def po_token_provider_available(self) -> bool:
        return bool(self.po_token_provider_detections)

    @property
    def po_token_provider_ready(self) -> bool:
        return (
            self.po_token_provider_available
            and self.po_token_backend.backend_present
            and self.po_token_backend.backend_healthy
        )

    def recommended_actions(self) -> list[str]:
        actions: list[str] = []
        for c in self.checks:
            if c.status in (DoctorStatus.WARN, DoctorStatus.FAIL) and c.detail:
                actions.append(c.detail)
        return actions

    def _status_for(self, category: str) -> DoctorStatus:
        for c in self.checks:
            if c.category == category:
                return c.status
        return DoctorStatus.WARN

    def summary_text(self) -> str:
        lines: list[str] = []
        for c in self.checks:
            lines.append(f"{_STATUS_ICON[c.status]}  [{c.category}] {c.message}")
        lines.append("")
        lines.append(f"{'Ready for public YouTube downloads':<36}: {self.ready_for_public_downloads}")
        lines.append(f"{'Cookies available for gated videos':<36}: {self.cookies_available_for_gated}")
        lines.append(
            f"{'PO Token Provider ready':<36}: "
            + ("yes" if self.po_token_provider_ready else "no")
        )
        actions = self.recommended_actions()
        if actions:
            lines.append("")
            lines.append("Recommended actions:")
            for a in actions:
                lines.append(f"  - {a}")
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 1. yt-dlp version
# ──────────────────────────────────────────────────────────────────────────────

# Keep in sync with requirements.txt's `yt-dlp[default]>=...` pin. This is
# the known *minimum*, not an attempt to track "latest" — there is no
# existing safe (offline, no-network) mechanism in this codebase for that.
MIN_YT_DLP_VERSION = "2026.6.9"


def _parse_calver(version_str: str) -> tuple[int, int, int]:
    """Parse a calendar-versioned string ("2026.6.9") into a comparable
    (year, month, day) tuple. Falls back to (0, 0, 0) for garbage input."""
    parts = re.findall(r"\d+", version_str or "")
    year  = int(parts[0]) if len(parts) > 0 else 0
    month = int(parts[1]) if len(parts) > 1 else 0
    day   = int(parts[2]) if len(parts) > 2 else 0
    return year, month, day


def check_yt_dlp_version(installed_version: Optional[str] = None) -> DoctorCheck:
    """``installed_version`` is injectable for tests; defaults to the
    real installed yt-dlp package."""
    if installed_version is None:
        try:
            import yt_dlp
            installed_version = yt_dlp.version.__version__
        except Exception as exc:  # pragma: no cover - yt-dlp is a hard dependency
            return _mk_check(
                "yt_dlp_version", DoctorStatus.FAIL,
                "doctor_yt_dlp_missing",
                detail_key="doctor_yt_dlp_missing_action",
                detail_params={"exc": exc},
            )

    if _parse_calver(installed_version) >= _parse_calver(MIN_YT_DLP_VERSION):
        return _mk_check(
            "yt_dlp_version", DoctorStatus.PASS,
            "doctor_yt_dlp_ok",
            {"installed": installed_version, "minimum": MIN_YT_DLP_VERSION},
        )
    return _mk_check(
        "yt_dlp_version", DoctorStatus.WARN,
        "doctor_yt_dlp_outdated",
        {"installed": installed_version, "minimum": MIN_YT_DLP_VERSION},
        detail_key="doctor_yt_dlp_outdated_action",
        detail_params={"minimum": MIN_YT_DLP_VERSION},
    )


# ──────────────────────────────────────────────────────────────────────────────
# 2. yt-dlp-ejs availability
# ──────────────────────────────────────────────────────────────────────────────

def check_yt_dlp_ejs(
    *, _find_spec: Callable[[str], object] = importlib.util.find_spec,
) -> DoctorCheck:
    """Detection only — uses find_spec (metadata lookup), never imports
    the module, so this cannot trigger any plugin-registration side effect."""
    try:
        spec = _find_spec("yt_dlp_ejs")
    except (ImportError, ValueError, ModuleNotFoundError):
        spec = None

    if spec is not None:
        return _mk_check("yt_dlp_ejs", DoctorStatus.PASS, "doctor_ejs_ok")
    return _mk_check(
        "yt_dlp_ejs", DoctorStatus.WARN,
        "doctor_ejs_missing",
        detail_key="doctor_ejs_missing_action",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 3. JavaScript runtime status
# ──────────────────────────────────────────────────────────────────────────────

def _runtime_path_is_bundled(runtime_path: Optional[str]) -> bool:
    """True when ``runtime_path`` lives inside a BananaFlow-bundled runtime dir.
    Best-effort — any failure means 'not bundled', never raises."""
    if not runtime_path:
        return False
    try:
        from core.runtime_components import find_bundled_runtime
        bundled = find_bundled_runtime()
        if bundled is None:
            return False
        return Path(runtime_path).resolve() == bundled[1].resolve()
    except Exception:
        return False


def check_js_runtimes() -> tuple[DoctorCheck, list[RuntimeStatus]]:
    """Mirrors utils.yt_dlp_opts's own runtime preference/selection so the
    Doctor can never report a different "selected" runtime than the one
    yt-dlp will actually be configured to use."""
    import shutil as _shutil
    from utils.yt_dlp_opts import (
        _JS_RUNTIME_EXE_NAMES,
        _JS_RUNTIMES_PREFERENCE,
        _NODE_MIN_MAJOR_VERSION,
        _get_node_version_output,
        _node_major_version,
    )

    statuses: list[RuntimeStatus] = []
    selected: Optional[str] = None
    selected_path: Optional[str] = None

    for name in _JS_RUNTIMES_PREFERENCE:
        path = _shutil.which(_JS_RUNTIME_EXE_NAMES[name])
        if not path:
            statuses.append(RuntimeStatus(name=name, found=False))
            continue

        if name == "node":
            version_out = _get_node_version_output(path).strip()
            major = _node_major_version(version_out)
            supported = major is not None and major >= _NODE_MIN_MAJOR_VERSION
            statuses.append(RuntimeStatus(name=name, found=True, supported=supported, version=version_out))
        else:
            statuses.append(RuntimeStatus(name=name, found=True, supported=True))

        if selected is None and statuses[-1].supported:
            selected = name
            selected_path = path

    if selected:
        detail_bits = ", ".join(
            f"{s.name}={'ok' if s.supported else ('missing' if not s.found else 'unsupported')}"
            for s in statuses
        )
        message_key = (
            "doctor_js_ok_bundled" if _runtime_path_is_bundled(selected_path)
            else "doctor_js_ok"
        )
        check = _mk_check(
            "js_runtime", DoctorStatus.PASS,
            message_key, {"selected": selected, "details": detail_bits},
        )
    else:
        node_status = next((s for s in statuses if s.name == "node"), None)
        if node_status and node_status.found and not node_status.supported:
            check = _mk_check(
                "js_runtime", DoctorStatus.FAIL,
                "doctor_js_node_too_old",
                {"version": node_status.version or "(unknown version)"},
                detail_key="doctor_js_action",
            )
        else:
            check = _mk_check(
                "js_runtime", DoctorStatus.FAIL,
                "doctor_js_none",
                detail_key="doctor_js_action",
            )

    return check, statuses


# ──────────────────────────────────────────────────────────────────────────────
# 4. Cookies diagnostics
# ──────────────────────────────────────────────────────────────────────────────

# Google/YouTube cookie names that only appear on a genuinely signed-in
# session. Presence is a signal, not proof the session is still valid.
_LOGIN_COOKIE_NAMES = frozenset({
    "LOGIN_INFO", "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
})


def _inspect_cookies_file(path: Path) -> CookieDiagnostics:
    from utils.url_cleaner import host_matches_domain

    diag = CookieDiagnostics(mode="file", file_name=path.name)

    if not path.exists():
        return diag
    diag.file_exists = True

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return diag  # exists but unreadable
    diag.file_readable = True

    cookie_line_count = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        cookie_line_count += 1

        # Only ever read the domain (parts[0]) and name (parts[5]).
        # parts[6] is the cookie value — intentionally never touched.
        domain = parts[0].lower().lstrip(".")
        name   = parts[5]

        # Exact/subdomain match, not a substring test: "youtube.com" in
        # domain would also match "youtube.com.evil.example".
        if host_matches_domain(domain, "youtube.com"):
            diag.has_youtube_domain_cookies = True
        if name in _LOGIN_COOKIE_NAMES and host_matches_domain(domain, "youtube.com", "google.com"):
            diag.has_likely_login_cookies = True

    diag.file_non_empty = cookie_line_count > 0
    return diag


def check_cookies(
    cookies_file: str = "",
    cookies_browser: str = "",
) -> tuple[DoctorCheck, CookieDiagnostics]:
    if cookies_browser:
        diag = CookieDiagnostics(mode="browser", browser=cookies_browser)
        return _mk_check(
            "cookies", DoctorStatus.PASS,
            "doctor_cookies_browser", {"browser": cookies_browser},
        ), diag

    if not cookies_file:
        diag = CookieDiagnostics(mode="none")
        return _mk_check("cookies", DoctorStatus.PASS, "doctor_cookies_none"), diag

    diag = _inspect_cookies_file(Path(cookies_file))
    name_param = {"name": diag.file_name}

    if not diag.file_exists:
        return _mk_check(
            "cookies", DoctorStatus.WARN,
            "doctor_cookies_file_missing", name_param,
            detail_key="doctor_cookies_reexport",
        ), diag
    if not diag.file_readable:
        return _mk_check(
            "cookies", DoctorStatus.WARN,
            "doctor_cookies_file_unreadable", name_param,
            detail_key="doctor_cookies_permissions",
        ), diag
    if not diag.file_non_empty:
        return _mk_check(
            "cookies", DoctorStatus.WARN,
            "doctor_cookies_file_empty", name_param,
            detail_key="doctor_cookies_reexport",
        ), diag
    if not diag.has_youtube_domain_cookies:
        return _mk_check(
            "cookies", DoctorStatus.WARN,
            "doctor_cookies_not_youtube", name_param,
            detail_key="doctor_cookies_reexport_on_youtube",
        ), diag
    if not diag.has_likely_login_cookies:
        return _mk_check(
            "cookies", DoctorStatus.WARN,
            "doctor_cookies_no_login", name_param,
            detail_key="doctor_cookies_reexport_signed_in",
        ), diag

    return _mk_check(
        "cookies", DoctorStatus.PASS,
        "doctor_cookies_login_ok", name_param,
    ), diag


# ──────────────────────────────────────────────────────────────────────────────
# 5. PO Token Provider detection and bundled backend readiness
#
# How yt-dlp actually discovers plugins (verified against
# yt_dlp/plugins.py + yt_dlp/YoutubeDL.py in the installed package, and
# with a local controlled check — see Phase 5A notes in
# docs/user-guide/youtube-doctor-qa.md / commit message):
#
#   * yt-dlp registers a namespace package `yt_dlp_plugins` the first
#     time `yt_dlp.extractor`/`yt_dlp.postprocessor` is imported
#     (yt_dlp/plugins.py: register_plugin_spec, PluginFinder inserted
#     into sys.meta_path). It searches yt-dlp's own config/plugin
#     directories *and* every directory already on sys.path — which
#     includes a normal pip install's site-packages.
#   * `YoutubeDL.__init__` calls `load_all_plugins()` automatically
#     (guarded by a once-per-process flag) — the *first* YoutubeDL()
#     instantiation in a process triggers plugin discovery with zero
#     extra configuration.
#   * `load_plugins()` imports (executes) every module found under
#     `yt_dlp_plugins.extractor.*`. A PO Token Provider plugin
#     self-registers via yt_dlp's `@register_provider` decorator as an
#     import side effect — it does not need to match the generic "IE"
#     extractor-class-name suffix to take effect.
#
#   => Conclusion: simply `pip install`-ing a conforming PO Token
#      Provider package is sufficient for yt-dlp to load the Python
#      plugin. Full packaged readiness also requires the Deno script
#      backend and the provider's official `server_home` extractor arg.
#
# YouTube Doctor keeps plugin detection filesystem-only, then separately
# validates the bundled Deno script backend with `generate_once.ts
# --version`. That local command proves the backend path is runnable
# without asking the provider to generate a PO Token.
# ──────────────────────────────────────────────────────────────────────────────

_POT_PROVIDER_DIST_NAME_HINTS = ("pot-provider", "getpot", "bgutil")


def _dist_getpot_module_names(dist) -> list[str]:
    """Return getpot_* module/package names this distribution installs
    under yt_dlp_plugins/extractor/, read purely from its RECORD
    metadata (dist.files) — no import, no execution."""
    names: list[str] = []
    for path in dist.files or ():
        parts = path.parts
        if len(parts) < 3 or parts[0] != "yt_dlp_plugins" or parts[1] != "extractor":
            continue
        name = parts[2]
        if name.endswith(".py"):
            name = name[:-3]
        if name and not name.startswith("_") and name.lower().startswith("getpot"):
            names.append(name)
    return names


def _detect_bundled_provider_modules() -> list[str]:
    """Provider modules shipped inside this BananaFlow build (filesystem-only).
    Returns [] on any import/scan failure so detection never raises."""
    try:
        from core.runtime_components import (
            find_bundled_plugins_dir,
            scan_bundled_provider_modules,
        )
        return scan_bundled_provider_modules(find_bundled_plugins_dir())
    except Exception:
        return []


def detect_po_token_provider(
    *,
    _find_spec: Callable[[str], object] = importlib.util.find_spec,
    _iter_modules: Callable = pkgutil.iter_modules,
    _distributions: Callable = importlib.metadata.distributions,
    _bundled_modules: Callable[[], list[str]] = _detect_bundled_provider_modules,
) -> list[ProviderDetection]:
    """
    Return every heuristic signal that a PO Token Provider plugin is
    installed. Pure metadata/filesystem inspection — never imports or
    executes a plugin, never generates, scrapes, stores, or uses a PO
    Token.

    Three independent, offline signals are combined:
      * "bundled" — a getpot_* module ships inside this BananaFlow build's
        own yt-dlp-plugins folder (see core.runtime_components). Full PO
        readiness additionally requires the bundled runtime/backend
        health check.
      * "distribution" — an installed package's own RECORD metadata
        lists a yt_dlp_plugins/extractor/getpot_* file (this also gives
        us the real installing package name), or — if RECORD didn't
        list it for some reason — its distribution name matches a known
        naming hint.
      * "namespace" — a yt_dlp_plugins.extractor.getpot_* module is
        reachable via sys.path / yt-dlp's plugin directories but isn't
        tied to any installed distribution (e.g. manually dropped into
        a yt-dlp plugin folder rather than pip-installed).
    """
    detections: list[ProviderDetection] = []
    seen_modules: set[str] = set()

    for module_name in _bundled_modules():
        seen_modules.add(module_name.lower())
        detections.append(ProviderDetection(
            method="bundled",
            module_name=f"yt_dlp_plugins.extractor.{module_name}",
            distribution_name="bundled with BananaFlow",
            bundled=True,
        ))

    try:
        for dist in _distributions():
            dist_name = dist.metadata.get("Name") or ""
            module_names = _dist_getpot_module_names(dist)
            if module_names:
                for module_name in module_names:
                    seen_modules.add(module_name.lower())
                    detections.append(ProviderDetection(
                        method="distribution",
                        module_name=f"yt_dlp_plugins.extractor.{module_name}",
                        distribution_name=dist_name,
                    ))
            elif dist_name and any(hint in dist_name.lower() for hint in _POT_PROVIDER_DIST_NAME_HINTS):
                # Name looks right but RECORD didn't list the module path
                # (unusual install layout) — still worth surfacing.
                detections.append(ProviderDetection(method="distribution", distribution_name=dist_name))
    except Exception:
        pass

    try:
        spec = _find_spec("yt_dlp_plugins.extractor")
    except (ImportError, ValueError, ModuleNotFoundError):
        spec = None
    if spec is not None and getattr(spec, "submodule_search_locations", None):
        for _finder, name, _ispkg in _iter_modules(spec.submodule_search_locations):
            if name.lower().startswith("getpot") and name.lower() not in seen_modules:
                detections.append(ProviderDetection(
                    method="namespace", module_name=f"yt_dlp_plugins.extractor.{name}",
                ))

    return detections


def _detect_bundled_backend_info():
    try:
        from core.runtime_components import detect_bundled_components
        return detect_bundled_components()
    except Exception:
        return None


def _run_bundled_provider_script_version(info) -> tuple[bool, str, str]:
    """Run bgutil's Deno script version check, not token generation."""
    try:
        from core.runtime_components import bundled_pot_provider_script_command
        cmd = bundled_pot_provider_script_command(info)
    except Exception as exc:
        return False, "", f"could not build health-check command ({exc})"
    if not cmd:
        return False, "", "health-check command is unavailable"

    env = os.environ.copy()
    cache_home = getattr(info, "provider_cache_home", None)
    if cache_home:
        env["XDG_CACHE_HOME"] = str(cache_home)
    env["DENO_NO_PROMPT"] = "1"
    env["DENO_NO_UPDATE_CHECK"] = "1"
    env["FORCE_COLOR"] = "false"

    # Deno is a console program; from a windowed build an unhidden launch
    # puts a black window on screen for the length of the health check.
    from utils.proc import run_hidden

    result = run_hidden(
        cmd, purpose="pot-provider-healthcheck", timeout=90, env=env,
    )
    if result.error or result.timed_out:
        return False, "", result.error

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    version = stdout.splitlines()[-1].strip() if stdout else ""
    if result.returncode == 0 and version:
        return True, version, ""
    reason = stderr or stdout or f"exit {result.returncode}"
    return False, version, reason


def check_po_token_backend(
    *,
    _components: Callable = _detect_bundled_backend_info,
    _run_script_version: Callable = _run_bundled_provider_script_version,
) -> ProviderBackendDiagnostics:
    info = _components()
    if info is None:
        return ProviderBackendDiagnostics(reason="bundled-component detection failed")

    diag = ProviderBackendDiagnostics(
        mode=getattr(info, "provider_backend_mode", "") or "",
        runtime_name=getattr(info, "js_runtime_name", "") or "",
        version=getattr(info, "provider_backend_version", "") or "",
        server_home=str(getattr(info, "provider_backend_dir", "") or ""),
        script_path=str(getattr(info, "provider_script_path", "") or ""),
    )
    diag.backend_present = bool(getattr(info, "has_bundled_provider_backend", False))
    if not diag.backend_present:
        diag.reason = "provider backend files are missing or incomplete"
        return diag
    if diag.runtime_name != "deno":
        diag.reason = "bundled Deno runtime is not selected"
        return diag

    ok, version, reason = _run_script_version(info)
    diag.backend_healthy = ok
    if version:
        diag.version = version
    diag.reason = reason
    return diag


def check_po_token_provider(
    *,
    _find_spec: Callable[[str], object] = importlib.util.find_spec,
    _iter_modules: Callable = pkgutil.iter_modules,
    _distributions: Callable = importlib.metadata.distributions,
    _bundled_modules: Callable[[], list[str]] = _detect_bundled_provider_modules,
    _backend_check: Callable[[], ProviderBackendDiagnostics] = lambda: ProviderBackendDiagnostics(),
) -> tuple[DoctorCheck, list[ProviderDetection]]:
    detections = detect_po_token_provider(
        _find_spec=_find_spec, _iter_modules=_iter_modules, _distributions=_distributions,
        _bundled_modules=_bundled_modules,
    )
    backend = _backend_check()
    if detections:
        methods = sorted({d.method for d in detections})
        is_bundled = any(d.bundled for d in detections)
        has_script_provider = any(
            (d.module_name or "").endswith("getpot_bgutil_script")
            for d in detections
        )
        if backend.backend_healthy and has_script_provider:
            return _mk_check(
                "po_token_provider", DoctorStatus.PASS,
                "doctor_pot_ready", {"version": backend.version or "unknown"},
            ), detections
        if backend.backend_healthy:
            return _mk_check(
                "po_token_provider", DoctorStatus.WARN,
                "doctor_pot_script_provider_missing",
                detail_key="doctor_pot_missing_action",
            ), detections
        if is_bundled:
            if backend.backend_present:
                return _mk_check(
                    "po_token_provider", DoctorStatus.WARN,
                    "doctor_pot_backend_unhealthy",
                    {"reason": backend.reason or "unknown failure"},
                    detail_key="doctor_pot_missing_action",
                ), detections
            if backend.runtime_name:
                return _mk_check(
                    "po_token_provider", DoctorStatus.WARN,
                    "doctor_pot_plugin_runtime_no_backend",
                    detail_key="doctor_pot_missing_action",
                ), detections
            return _mk_check(
                "po_token_provider", DoctorStatus.WARN,
                "doctor_pot_plugin_only",
                detail_key="doctor_pot_missing_action",
            ), detections
        names = sorted({d.distribution_name or d.module_name for d in detections if (d.distribution_name or d.module_name)})
        name_note = f" ({', '.join(names)})" if names else ""
        return _mk_check(
            "po_token_provider", DoctorStatus.WARN,
            "doctor_pot_installed_no_backend",
            {"name_note": name_note, "methods": "/".join(methods)},
            detail_key="doctor_pot_missing_action",
        ), detections
    return _mk_check(
        "po_token_provider", DoctorStatus.WARN,
        "doctor_pot_missing",
        detail_key="doctor_pot_missing_action",
    ), detections


# ──────────────────────────────────────────────────────────────────────────────
# 6. YouTube reliability mode status
# ──────────────────────────────────────────────────────────────────────────────

def check_reliability_mode(youtube_reliability_mode: str = "conservative") -> DoctorCheck:
    if youtube_reliability_mode == "conservative":
        return _mk_check(
            "youtube_reliability_mode", DoctorStatus.PASS,
            "doctor_reliability_conservative",
        )
    return _mk_check(
        "youtube_reliability_mode", DoctorStatus.WARN,
        "doctor_reliability_fast",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 7. Top-level entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_youtube_doctor(
    cookies_file: str = "",
    cookies_browser: str = "",
    youtube_reliability_mode: str = "conservative",
) -> YoutubeDoctorReport:
    """
    Run every YouTube reliability check and return a structured report.
    Offline only — makes no network calls, never mutates app state, never
    reads/uses PO Tokens or cookie values.
    """
    yt_dlp_check = check_yt_dlp_version()
    ejs_check = check_yt_dlp_ejs()
    runtime_check, _runtime_statuses = check_js_runtimes()
    cookies_check, cookies_diag = check_cookies(cookies_file, cookies_browser)
    pot_backend = check_po_token_backend()
    pot_check, pot_detections = check_po_token_provider(
        _backend_check=lambda: pot_backend,
    )
    reliability_check = check_reliability_mode(youtube_reliability_mode)

    return YoutubeDoctorReport(
        checks=[
            yt_dlp_check,
            ejs_check,
            runtime_check,
            cookies_check,
            pot_check,
            reliability_check,
        ],
        cookies=cookies_diag,
        po_token_provider_detections=pot_detections,
        po_token_backend=pot_backend,
    )
