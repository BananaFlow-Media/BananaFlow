"""Phase 4 (reproducible builds / supply chain) static workflow gates.

FFmpeg is bundled into every release build, so its download must be pinned
to an exact, immutable version and verified by checksum before use — never
downloaded from a "latest" alias with no integrity check. These are text
assertions over the workflow, the only way to test a workflow without
running it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-windows.yml"
MACOS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-macos.yml"
DENO_FETCH_SCRIPT = REPO_ROOT / "scripts" / "fetch_deno_runtime.ps1"
MACOS_BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_macos.sh"


@pytest.fixture(scope="module")
def windows_text() -> str:
    return WINDOWS_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def macos_text() -> str:
    return MACOS_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def macos_build_script_text() -> str:
    return MACOS_BUILD_SCRIPT.read_text(encoding="utf-8")


def _ffmpeg_step(text: str, heading: str) -> str:
    body = text.split(heading, 1)[1]
    return body.split("\n      - name:", 1)[0]


class TestWindowsFfmpegPinning:

    def test_does_not_use_the_mutable_latest_tag(self, windows_text: str):
        step = _ffmpeg_step(windows_text, "Download FFmpeg (LGPL) for bundling")
        assert "/releases/download/latest/" not in step, (
            "the mutable 'latest' release alias must not be used for a "
            "bundled binary — it can change what a release ships with no audit trail"
        )
        assert "ffmpeg-master-latest" not in step

    def test_pins_an_exact_tag_and_asset(self, windows_text: str):
        step = _ffmpeg_step(windows_text, "Download FFmpeg (LGPL) for bundling")
        assert re.search(r'\$tag\s*=\s*"autobuild-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}"', step), (
            "an exact, dated BtbN release tag must be pinned"
        )
        assert re.search(r'\$asset\s*=\s*"ffmpeg-[^"]+-win64-lgpl\.zip"', step)

    def test_verifies_a_sha256_checksum_and_fails_closed(self, windows_text: str):
        step = _ffmpeg_step(windows_text, "Download FFmpeg (LGPL) for bundling")
        assert re.search(r'\$expectedSha256\s*=\s*"[0-9a-f]{64}"', step), (
            "an expected SHA-256 must be pinned alongside the URL"
        )
        assert "Get-FileHash" in step
        assert "checksum mismatch" in step.lower()
        assert "throw" in step, "a checksum mismatch must fail the build, not warn"

    def test_removes_the_downloaded_zip_after_verification(self, windows_text: str):
        step = _ffmpeg_step(windows_text, "Download FFmpeg (LGPL) for bundling")
        assert "Remove-Item -Recurse -Force ffmpeg-tmp, ffmpeg-lgpl.zip" in step


class TestMacosFfmpegPinning:

    def test_does_not_use_the_mutable_getrelease_alias(self, macos_text: str):
        step = _ffmpeg_step(macos_text, "Stage LGPL FFmpeg (arm64) for bundling")
        download_line = next(
            line for line in step.splitlines() if "curl" in line and "evermeet.cx" in line
        )
        assert "getrelease" not in download_line, (
            "evermeet.cx's 'getrelease' endpoint always serves whatever is "
            "currently published — an exact version must be pinned instead"
        )

    def test_pins_an_exact_version(self, macos_text: str):
        step = _ffmpeg_step(macos_text, "Stage LGPL FFmpeg (arm64) for bundling")
        assert re.search(r'version="\d+\.\d+(\.\d+)?"', step)
        assert re.search(r"\$tool-\$version\.zip", step)

    def test_verifies_a_sha256_checksum_per_tool_and_fails_closed(self, macos_text: str):
        # A plain `case`, not an associative array: macOS runners execute
        # this via bash 3.2 (Apple's frozen GPLv2 release), which has no
        # `declare -A` support -- see test_release_workflow_policy.py's
        # test_macos_workflow_avoids_bash_4_only_syntax.
        step = _ffmpeg_step(macos_text, "Stage LGPL FFmpeg (arm64) for bundling")
        assert re.search(r'ffmpeg\)\s+expected="[0-9a-f]{64}"', step)
        assert re.search(r'ffprobe\)\s+expected="[0-9a-f]{64}"', step)
        assert "shasum -a 256" in step
        assert "checksum mismatch" in step.lower()
        assert re.search(r"exit 1", step), "a checksum mismatch must fail the build, not warn"


class TestMacosBuildScriptFfmpegPinning:
    """`scripts/build_macos.sh` stages FFmpeg into the very same
    `packaging/ffmpeg/` directory PyInstaller bakes into the shipped .app.
    Phase 4 pinned both release *workflows* but left this script on
    evermeet.cx's mutable `getrelease` alias with no checksum at all, so a
    locally-built .app could bundle an unverified media binary. The script
    duplicates the workflow's pin (it cannot read a YAML step), so the
    cross-check below is what actually keeps the two honest -- the same
    pattern test_dependency_versions.py uses for the bgutil pin.
    """

    def test_does_not_use_the_mutable_getrelease_alias(self, macos_build_script_text: str):
        download_line = next(
            line for line in macos_build_script_text.splitlines()
            if "curl" in line and "evermeet.cx" in line
        )
        assert "getrelease" not in download_line, (
            "evermeet.cx's 'getrelease' endpoint always serves whatever is "
            "currently published -- an exact version must be pinned instead"
        )
        assert "$tool-$FFMPEG_VERSION.zip" in download_line

    def test_verifies_a_sha256_per_tool_and_fails_closed(self, macos_build_script_text: str):
        assert "shasum -a 256" in macos_build_script_text
        assert "checksum mismatch" in macos_build_script_text.lower()
        staging = macos_build_script_text.split("FFmpeg (LGPL) staging", 1)[1]
        assert re.search(r'\$actual"?\s*!=\s*"?\$expected', staging), (
            "the computed hash must be compared against the pinned one"
        )
        assert "exit 1" in staging, (
            "a checksum mismatch must fail the build, not warn and continue"
        )

    def test_pin_matches_the_release_workflow(
        self, macos_build_script_text: str, macos_text: str
    ):
        """The authoritative release path is release-macos.yml; this script
        must never drift away from it and stage a different FFmpeg."""
        step = _ffmpeg_step(macos_text, "Stage LGPL FFmpeg (arm64) for bundling")

        workflow_version = re.search(r'version="([\d.]+)"', step)
        assert workflow_version, "could not find the workflow's pinned version"
        script_version = re.search(r'FFMPEG_VERSION="([\d.]+)"', macos_build_script_text)
        assert script_version, "could not find build_macos.sh's FFMPEG_VERSION"
        assert script_version.group(1) == workflow_version.group(1), (
            f"build_macos.sh pins FFmpeg {script_version.group(1)} but "
            f"release-macos.yml pins {workflow_version.group(1)} -- keep them in sync."
        )

        for tool, script_var in (("ffmpeg", "FFMPEG_SHA256"), ("ffprobe", "FFPROBE_SHA256")):
            workflow_hash = re.search(rf'{tool}\)\s+expected="([0-9a-f]{{64}})"', step)
            assert workflow_hash, f"could not find the workflow's {tool} SHA-256"
            script_hash = re.search(
                rf'{script_var}="([0-9a-f]{{64}})"', macos_build_script_text
            )
            assert script_hash, f"could not find build_macos.sh's {script_var}"
            assert script_hash.group(1) == workflow_hash.group(1), (
                f"build_macos.sh's {script_var} does not match release-macos.yml's "
                f"pinned {tool} hash -- keep them in sync."
            )


class TestDenoRuntimePinning:
    """Issue #31: the Deno runtime checksum used to be fetched from a
    .sha256sum asset on the SAME GitHub release as the binary it verifies --
    proving only that the download matched what that page currently serves,
    not that the release itself is trustworthy. Fixed to pin real,
    out-of-band-verified hashes in the script instead.

    The first round of these tests only read the .ps1 as text and regex'd it,
    which cannot tell a script that verifies checksums from one that merely
    contains the word. The behavioral tests live in TestDenoScriptExecution
    below; what stays here is the structural claim that no second, same-
    release fetch exists, which is genuinely a property of the source.
    """

    def _script_text(self) -> str:
        return DENO_FETCH_SCRIPT.read_text(encoding="utf-8")

    def test_does_not_fetch_the_checksum_from_the_same_release(self):
        text = self._script_text()
        assert text.count("Invoke-WebRequest") == 1, (
            "only the zip itself should be fetched with Invoke-WebRequest, "
            "not a second same-release checksum asset"
        )
        assert not re.search(r"Invoke-WebRequest[^\n]*sha256sum", text), (
            "must not fetch a .sha256sum asset from the release being verified"
        )

    def test_pins_a_real_sha256_for_the_default_version(self):
        text = self._script_text()
        match = re.search(r"\$Version\s*=\s*'([\d.]+)'", text)
        assert match, "could not find the default -Version pin"
        default_version = match.group(1)

        pinned = re.search(r"\$PinnedHashes\s*=\s*@\{(.*?)\}", text, re.S)
        assert pinned, "could not find a $PinnedHashes table"
        assert re.search(
            rf"'{re.escape(default_version)}'\s*=\s*'[0-9a-f]{{64}}'", pinned.group(1)
        ), f"no pinned SHA-256 for the default version {default_version!r}"

    def test_the_unpinned_version_message_does_not_send_you_back_to_the_same_release(self):
        """The refuse-unpinned message used to tell the maintainer to fetch
        "$ZipUrl.sha256sum from a source you trust independently" -- which is
        the same release's own asset. Following that advice reintroduces
        exactly the trust loop #31 was filed about."""
        text = self._script_text()
        message = re.search(r"No pinned SHA-256 for Deno(.*?)\n\s*\)", text, re.S)
        assert message, "could not find the unpinned-version error message"
        assert "$ZipUrl.sha256sum" not in message.group(1), (
            "the guidance must not point at the release's own .sha256sum asset "
            "as an independent source -- it is not one"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Behavioral coverage: actually run the script.
# ──────────────────────────────────────────────────────────────────────────────

def _powershell() -> str | None:
    for exe in ("pwsh", "powershell"):
        if shutil.which(exe):
            return exe
    return None


@pytest.mark.skipif(_powershell() is None, reason="no PowerShell available to run the script")
class TestDenoScriptExecution:
    """Issue #31, behavioral half: these invoke fetch_deno_runtime.ps1 and
    assert on what it does, not on what its source says. All offline — the
    -ArchivePath parameter verifies a caller-supplied zip through the exact
    same pin and fail-closed check as a downloaded one."""

    def _run(self, tmp_path, *args):
        return subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-File", str(DENO_FETCH_SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tmp_path,
        )

    def _pinned_default(self) -> tuple[str, str]:
        text = DENO_FETCH_SCRIPT.read_text(encoding="utf-8")
        version = re.search(r"\$Version\s*=\s*'([\d.]+)'", text).group(1)
        pinned = re.search(r"\$PinnedHashes\s*=\s*@\{(.*?)\}", text, re.S).group(1)
        digest = re.search(rf"'{re.escape(version)}'\s*=\s*'([0-9a-f]{{64}})'", pinned).group(1)
        return version, digest

    def test_an_unpinned_version_is_refused_before_anything_is_downloaded(self, tmp_path):
        """The whole point of the pin: an unknown version must stop the build,
        not silently fall back to trusting whatever the release page serves."""
        result = self._run(tmp_path, "-Version", "0.0.0-not-a-real-release")

        assert result.returncode != 0, "an unpinned version must fail the build"
        assert "No pinned SHA-256" in result.stderr, result.stderr
        # Nothing was fetched: the refusal happens before the download.
        assert "Fetching Deno" not in result.stdout

    def test_a_tampered_archive_fails_closed(self, tmp_path):
        """A zip whose bytes do not match the pin must be rejected, whatever
        it claims to be."""
        version, _ = self._pinned_default()
        archive = tmp_path / "deno.zip"
        archive.write_bytes(b"not the real deno release")

        result = self._run(tmp_path, "-Version", version, "-ArchivePath", str(archive))

        assert result.returncode != 0, "a checksum mismatch must fail the build"
        assert "SHA-256 mismatch" in result.stderr, result.stderr
        assert archive.exists(), (
            "a caller-supplied archive must not be deleted — only one this "
            "script fetched itself"
        )

    def test_the_pinned_hash_is_the_one_actually_enforced(self, tmp_path):
        """Guards against a pin that is present but unused: the mismatch
        message must quote the pinned digest as the expected value, so a
        change to $PinnedHashes provably changes what is accepted."""
        version, digest = self._pinned_default()
        archive = tmp_path / "deno.zip"
        archive.write_bytes(b"wrong bytes")

        result = self._run(tmp_path, "-Version", version, "-ArchivePath", str(archive))

        assert digest in result.stderr, (
            f"the enforced expected hash should be the pinned {digest!r}; "
            f"got: {result.stderr}"
        )
        actual = hashlib.sha256(b"wrong bytes").hexdigest()
        assert actual in result.stderr.lower(), (
            "the mismatch message should report the archive's real digest"
        )

    def test_a_matching_archive_passes_verification(self, tmp_path):
        """The positive path: an archive whose digest equals the pin must get
        past verification. It then fails on the archive's *contents* (no
        deno.exe inside), which proves verification itself succeeded rather
        than the script rejecting everything unconditionally."""
        content = b"a zip that is not really a zip"
        archive = tmp_path / "deno.zip"
        archive.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()

        # Point the script's pin table at this archive by asking for a version
        # whose pinned hash we generate: patch a temp copy of the script.
        patched = tmp_path / "fetch_deno_runtime.ps1"
        text = DENO_FETCH_SCRIPT.read_text(encoding="utf-8")
        text = re.sub(r"(\$PinnedHashes\s*=\s*@\{)",
                      rf"\1\n    '9.9.9' = '{digest}'", text, count=1)
        patched.write_text(text, encoding="utf-8")

        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-File", str(patched),
             "-Version", "9.9.9", "-ArchivePath", str(archive)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=tmp_path,
        )

        assert "Checksum verified" in result.stdout, (
            f"a matching archive must pass verification; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert "SHA-256 mismatch" not in result.stderr
