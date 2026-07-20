"""
tests/test_stage_pot_provider.py  –  PO Token Provider staging script
========================================================================
packaging/stage_pot_provider.py copies an installed bgutil-ytdlp-pot-
provider's yt_dlp_plugins/extractor/getpot_*.py files into
packaging/yt-dlp-plugins/ for bundling into the packaged EXE. These
tests fake the installed distribution (importlib.metadata) so they never
depend on whether the GPL v3 provider package actually happens to be
installed in the environment running the suite.

packaging/ can't be imported as a normal package — its name collides
with the real PyPI ``packaging`` library already on sys.path — so the
script is loaded directly from its file path.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "packaging" / "stage_pot_provider.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("stage_pot_provider_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def stage_mod(tmp_path, monkeypatch):
    module = _load_module()
    # Redirect staging output to a throwaway directory so tests never
    # touch the real packaging/yt-dlp-plugins/.
    fake_root = tmp_path / "yt-dlp-plugins" / module.DIST_NAME
    monkeypatch.setattr(module, "STAGING_ROOT", fake_root)
    return module


class _FakeDistribution:
    """Minimal importlib.metadata.Distribution stand-in."""

    def __init__(self, version: str, files: list, site_packages: Path):
        self.version = version
        self.files = files
        self._site_packages = site_packages

    def locate_file(self, relative):
        return self._site_packages / relative


def _make_real_site_packages(tmp_path: Path) -> tuple[Path, list]:
    """Create a fake site-packages layout matching the real wheel's
    contents (verified against the actual 1.3.1 wheel: 3 extractor files
    + __pycache__ + dist-info — only the .py extractor files should ever
    get staged)."""
    from pathlib import PurePosixPath

    site_packages = tmp_path / "site-packages"
    extractor_dir = site_packages / "yt_dlp_plugins" / "extractor"
    extractor_dir.mkdir(parents=True)
    pycache_dir = extractor_dir / "__pycache__"
    pycache_dir.mkdir()

    py_files = ["getpot_bgutil.py", "getpot_bgutil_http.py", "getpot_bgutil_script.py"]
    for name in py_files:
        (extractor_dir / name).write_text(f"# fake {name}\n", encoding="utf-8")
    (pycache_dir / "getpot_bgutil.cpython-312.pyc").write_bytes(b"\x00\x01")

    dist_info = site_packages / "bgutil_ytdlp_pot_provider-1.3.1.dist-info"
    dist_info.mkdir()
    for name in ("METADATA", "RECORD", "WHEEL", "INSTALLER", "REQUESTED"):
        (dist_info / name).write_text("", encoding="utf-8")

    record_paths = [
        PurePosixPath(f"yt_dlp_plugins/extractor/__pycache__/{p.stem}.cpython-312.pyc")
        for p in [Path(n) for n in py_files]
    ] + [
        PurePosixPath(f"yt_dlp_plugins/extractor/{name}") for name in py_files
    ] + [
        PurePosixPath(f"{dist_info.name}/{name}")
        for name in ("INSTALLER", "METADATA", "RECORD", "REQUESTED", "WHEEL")
    ]
    return site_packages, record_paths


class TestStageHappyPath:

    def test_stages_only_script_mode_extractor_py_files(self, stage_mod, tmp_path, monkeypatch):
        site_packages, record_paths = _make_real_site_packages(tmp_path)
        dist = _FakeDistribution("1.3.1", record_paths, site_packages)
        monkeypatch.setattr(
            stage_mod.importlib.metadata, "distribution", lambda name: dist
        )

        result = stage_mod.stage()

        assert result is True
        dest = stage_mod.STAGING_ROOT / "yt_dlp_plugins" / "extractor"
        staged = sorted(p.name for p in dest.iterdir())
        assert staged == ["getpot_bgutil.py", "getpot_bgutil_script.py"]
        # Never stage bytecode or dist-info — only the real plugin source.
        assert not any(p.suffix == ".pyc" for p in dest.iterdir())
        assert not (dest / "getpot_bgutil_http.py").exists()

    def test_staged_content_matches_source(self, stage_mod, tmp_path, monkeypatch):
        site_packages, record_paths = _make_real_site_packages(tmp_path)
        dist = _FakeDistribution("1.3.1", record_paths, site_packages)
        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", lambda name: dist)

        stage_mod.stage()

        staged_file = stage_mod.STAGING_ROOT / "yt_dlp_plugins" / "extractor" / "getpot_bgutil.py"
        assert staged_file.read_text(encoding="utf-8") == "# fake getpot_bgutil.py\n"

    def test_rerun_is_idempotent_and_clears_stale_files(self, stage_mod, tmp_path, monkeypatch):
        site_packages, record_paths = _make_real_site_packages(tmp_path)
        dist = _FakeDistribution("1.3.1", record_paths, site_packages)
        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", lambda name: dist)

        stage_mod.stage()
        # Simulate a stale file left over from a previous, different version.
        stale = stage_mod.STAGING_ROOT / "yt_dlp_plugins" / "extractor" / "getpot_old_removed.py"
        stale.write_text("stale", encoding="utf-8")

        stage_mod.stage()

        dest = stage_mod.STAGING_ROOT / "yt_dlp_plugins" / "extractor"
        assert not (dest / "getpot_old_removed.py").exists()
        assert (dest / "getpot_bgutil.py").exists()

    def test_matches_the_real_layout_core_runtime_components_expects(self, stage_mod, tmp_path, monkeypatch):
        """End-to-end: what stage() produces must be exactly what
        core.runtime_components.scan_bundled_provider_modules looks for."""
        site_packages, record_paths = _make_real_site_packages(tmp_path)
        dist = _FakeDistribution("1.3.1", record_paths, site_packages)
        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", lambda name: dist)
        stage_mod.stage()

        from core.runtime_components import scan_bundled_provider_modules
        found = scan_bundled_provider_modules(stage_mod.STAGING_ROOT.parent)
        assert found == ["getpot_bgutil", "getpot_bgutil_script"]


class TestStageNotInstalled:

    def test_returns_false_and_prints_guidance_when_not_installed(self, stage_mod, monkeypatch, capsys):
        def _raise(name):
            raise stage_mod.importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", _raise)

        result = stage_mod.stage()

        assert result is False
        captured = capsys.readouterr()
        assert "not installed" in captured.err
        assert "po-token" in captured.err or "pip install" in captured.err
        assert not stage_mod.STAGING_ROOT.exists()

    def test_no_op_never_raises(self, stage_mod, monkeypatch):
        """The function reports a missing package cleanly; main() decides
        whether that should fail the public packaging run."""
        def _raise(name):
            raise stage_mod.importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", _raise)
        stage_mod.stage()  # must not raise


class TestStageMalformedRecord:

    def test_no_extractor_files_in_record_returns_false(self, stage_mod, tmp_path, monkeypatch):
        site_packages = tmp_path / "site-packages"
        site_packages.mkdir()
        dist = _FakeDistribution("9.9.9", files=[], site_packages=site_packages)
        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", lambda name: dist)

        result = stage_mod.stage()
        assert result is False
        assert not stage_mod.STAGING_ROOT.exists()

    def test_ignores_underscore_prefixed_and_non_py_files(self, stage_mod, tmp_path, monkeypatch):
        from pathlib import PurePosixPath

        site_packages = tmp_path / "site-packages"
        extractor_dir = site_packages / "yt_dlp_plugins" / "extractor"
        extractor_dir.mkdir(parents=True)
        (extractor_dir / "getpot_bgutil.py").write_text("base", encoding="utf-8")
        (extractor_dir / "getpot_bgutil_script.py").write_text("script", encoding="utf-8")
        (extractor_dir / "getpot_bgutil_http.py").write_text("http", encoding="utf-8")
        (extractor_dir / "_private_helper.py").write_text("ignored", encoding="utf-8")
        (extractor_dir / "getpot_bgutil.pyi").write_text("ignored", encoding="utf-8")

        record_paths = [
            PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil.py"),
            PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil_script.py"),
            PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil_http.py"),
            PurePosixPath("yt_dlp_plugins/extractor/_private_helper.py"),
            PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil.pyi"),
        ]
        dist = _FakeDistribution("1.0.0", record_paths, site_packages)
        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", lambda name: dist)

        stage_mod.stage()

        dest = stage_mod.STAGING_ROOT / "yt_dlp_plugins" / "extractor"
        assert sorted(p.name for p in dest.iterdir()) == [
            "getpot_bgutil.py",
            "getpot_bgutil_script.py",
        ]

    def test_requires_both_script_mode_provider_files(self, stage_mod, tmp_path, monkeypatch):
        from pathlib import PurePosixPath

        site_packages = tmp_path / "site-packages"
        extractor_dir = site_packages / "yt_dlp_plugins" / "extractor"
        extractor_dir.mkdir(parents=True)
        (extractor_dir / "getpot_bgutil.py").write_text("base", encoding="utf-8")

        record_paths = [
            PurePosixPath("yt_dlp_plugins/extractor/getpot_bgutil.py"),
        ]
        dist = _FakeDistribution("1.0.0", record_paths, site_packages)
        monkeypatch.setattr(stage_mod.importlib.metadata, "distribution", lambda name: dist)

        assert stage_mod.stage() is False
        assert not stage_mod.STAGING_ROOT.exists()


class TestStageBackend:

    def _fake_source_zip(self, tmp_path: Path, stage_mod) -> Path:
        archive = tmp_path / "source.zip"
        prefix = f"{stage_mod.DIST_NAME}-{stage_mod.PROVIDER_VERSION}/"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(prefix + "LICENSE", "GPL\n")
            zf.writestr(prefix + "README.md", "readme\n")
            zf.writestr(prefix + "server/package.json", '{"version": "1.3.1"}\n')
            zf.writestr(prefix + "server/deno.lock", "{}\n")
            zf.writestr(prefix + "server/src/generate_once.ts", "// fake\n")
        return archive

    def test_stages_backend_source_and_installs_prod_deps(self, stage_mod, tmp_path, monkeypatch):
        backend_root = tmp_path / "pot-provider-backend" / stage_mod.DIST_NAME
        server_home = backend_root / "server"
        deno = tmp_path / "runtime" / ("deno.exe" if sys.platform == "win32" else "deno")
        deno.parent.mkdir(parents=True)
        deno.write_text("fake", encoding="utf-8")

        monkeypatch.setattr(stage_mod, "BACKEND_ROOT", backend_root)
        monkeypatch.setattr(stage_mod, "SERVER_HOME", server_home)
        monkeypatch.setattr(stage_mod, "DOWNLOAD_CACHE", tmp_path / "cache")
        monkeypatch.setattr(stage_mod, "_deno_path", lambda: deno)
        monkeypatch.setattr(stage_mod, "_npm_path", lambda: "npm")
        monkeypatch.setattr(stage_mod, "_download_source_archive", lambda: self._fake_source_zip(tmp_path, stage_mod))

        calls = []

        def fake_run(cmd, cwd, env, text):
            calls.append((cmd, cwd, env, text))
            (server_home / "node_modules").mkdir(parents=True, exist_ok=True)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(stage_mod.subprocess, "run", fake_run)

        assert stage_mod.stage_backend() is True

        assert (server_home / "src" / "generate_once.ts").exists()
        assert (server_home / "node_modules").is_dir()
        assert calls
        assert calls[0][0][:2] == ["npm", "ci"]
        assert "--omit=dev" in calls[0][0]

    def test_backend_staging_requires_bundled_deno(self, stage_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(stage_mod, "_deno_path", lambda: tmp_path / "missing-deno.exe")

        assert stage_mod.stage_backend() is False

    def test_backend_staging_requires_build_time_npm(self, stage_mod, tmp_path, monkeypatch):
        deno = tmp_path / "runtime" / ("deno.exe" if sys.platform == "win32" else "deno")
        deno.parent.mkdir(parents=True)
        deno.write_text("fake", encoding="utf-8")
        monkeypatch.setattr(stage_mod, "_deno_path", lambda: deno)
        monkeypatch.setattr(stage_mod, "_npm_path", lambda: "")

        assert stage_mod.stage_backend() is False


def _zip_of(path: Path, files: dict[str, bytes], **writer_options) -> Path:
    """Build a zip holding exactly `files`. writer_options let a test change
    how it is packed without changing what it contains."""
    import zipfile
    mode = writer_options.pop("compression", zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(path, "w", compression=mode, **writer_options) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return path


_PROVIDER_FILES = {
    "bgutil-ytdlp-pot-provider-1.3.1/README.md": b"# provider\n",
    "bgutil-ytdlp-pot-provider-1.3.1/server/src/generate_once.ts": b"export {};\n",
    "bgutil-ytdlp-pot-provider-1.3.1/plugin/getpot_bgutil.py": b"# plugin\n",
}


class TestSourceArchiveVerification:
    """F-013 / issue #29: the source archive download had no independently
    verifiable hash -- a compromised mirror/CDN or a corrupted transfer would
    be silently staged into the GPL v3 backend.

    The first fix pinned the zip's bytes. GitHub generates
    /archive/refs/tags/*.zip on demand and does not promise byte-stability, so
    that pin could break the build with a "tamper" message for no reason at
    all. The pin is now over the archive's contents.
    """

    def _pin(self, stage_mod, monkeypatch, digest: str, version: str | None = None):
        version = version or stage_mod.PROVIDER_VERSION
        monkeypatch.setattr(stage_mod, "PROVIDER_VERSION", version)
        monkeypatch.setattr(stage_mod, "PINNED_SOURCE_TREES", {version: digest})

    def test_matching_archive_is_accepted(self, stage_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(stage_mod, "DOWNLOAD_CACHE", tmp_path)
        archive = _zip_of(tmp_path / stage_mod.SOURCE_ZIP.name, _PROVIDER_FILES)
        monkeypatch.setattr(stage_mod, "SOURCE_ZIP", archive)
        self._pin(stage_mod, monkeypatch, stage_mod._tree_digest_of(archive))

        assert stage_mod._download_source_archive() == archive
        assert archive.exists()

    def test_repacking_the_same_files_does_not_change_the_digest(self, stage_mod, tmp_path):
        """The whole reason for a content digest: GitHub re-packing the same
        tag must not read as tampering."""
        import zipfile
        deflated = _zip_of(tmp_path / "a.zip", _PROVIDER_FILES,
                           compression=zipfile.ZIP_DEFLATED)
        stored = _zip_of(tmp_path / "b.zip", _PROVIDER_FILES,
                         compression=zipfile.ZIP_STORED)

        assert deflated.read_bytes() != stored.read_bytes(), (
            "the two archives must genuinely differ byte-wise, or this test "
            "proves nothing"
        )
        assert stage_mod._tree_digest_of(deflated) == stage_mod._tree_digest_of(stored)

    def test_member_order_does_not_change_the_digest(self, stage_mod, tmp_path):
        forward = _zip_of(tmp_path / "f.zip", _PROVIDER_FILES)
        reversed_files = dict(reversed(list(_PROVIDER_FILES.items())))
        backward = _zip_of(tmp_path / "b.zip", reversed_files)

        assert stage_mod._tree_digest_of(forward) == stage_mod._tree_digest_of(backward)

    def test_changing_a_single_byte_of_content_changes_the_digest(self, stage_mod, tmp_path):
        """The security property: different source must never digest the same."""
        original = _zip_of(tmp_path / "a.zip", _PROVIDER_FILES)
        tampered_files = dict(_PROVIDER_FILES)
        tampered_files["bgutil-ytdlp-pot-provider-1.3.1/server/src/generate_once.ts"] = (
            b"export {};\n// backdoor\n")
        tampered = _zip_of(tmp_path / "b.zip", tampered_files)

        assert stage_mod._tree_digest_of(original) != stage_mod._tree_digest_of(tampered)

    def test_renaming_a_file_changes_the_digest(self, stage_mod, tmp_path):
        """Paths are digested alongside contents, so moving code to a
        different location is not invisible."""
        original = _zip_of(tmp_path / "a.zip", _PROVIDER_FILES)
        moved = dict(_PROVIDER_FILES)
        moved["bgutil-ytdlp-pot-provider-1.3.1/server/src/evil.ts"] = moved.pop(
            "bgutil-ytdlp-pot-provider-1.3.1/server/src/generate_once.ts")

        assert stage_mod._tree_digest_of(original) != stage_mod._tree_digest_of(
            _zip_of(tmp_path / "b.zip", moved))

    def test_mismatched_cached_archive_fails_closed_and_is_removed(self, stage_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(stage_mod, "DOWNLOAD_CACHE", tmp_path)
        archive = _zip_of(tmp_path / stage_mod.SOURCE_ZIP.name, _PROVIDER_FILES)
        monkeypatch.setattr(stage_mod, "SOURCE_ZIP", archive)
        self._pin(stage_mod, monkeypatch, "0" * 64)

        with pytest.raises(RuntimeError, match="tree digest mismatch"):
            stage_mod._download_source_archive()

        assert not archive.exists(), "a failed-verification archive must not be left staged"

    def test_freshly_downloaded_archive_is_verified_too(self, stage_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(stage_mod, "DOWNLOAD_CACHE", tmp_path)
        archive = tmp_path / stage_mod.SOURCE_ZIP.name
        monkeypatch.setattr(stage_mod, "SOURCE_ZIP", archive)
        self._pin(stage_mod, monkeypatch, "0" * 64)

        def fake_urlretrieve(url, dest):
            _zip_of(Path(dest), {"whatever/the-network-returned.txt": b"x"})

        monkeypatch.setattr(stage_mod.urllib.request, "urlretrieve", fake_urlretrieve)

        with pytest.raises(RuntimeError, match="tree digest mismatch"):
            stage_mod._download_source_archive()

        assert not archive.exists()

    def test_a_corrupt_archive_is_rejected_not_crashed_on(self, stage_mod, tmp_path, monkeypatch):
        monkeypatch.setattr(stage_mod, "DOWNLOAD_CACHE", tmp_path)
        archive = tmp_path / stage_mod.SOURCE_ZIP.name
        archive.write_bytes(b"a truncated download, not a zip at all")
        monkeypatch.setattr(stage_mod, "SOURCE_ZIP", archive)
        self._pin(stage_mod, monkeypatch, "0" * 64)

        with pytest.raises(RuntimeError, match="not a readable zip"):
            stage_mod._download_source_archive()

        assert not archive.exists(), (
            "an unreadable cached archive must be removed so the next run "
            "re-downloads rather than failing forever"
        )

    def test_an_unpinned_version_is_refused_before_any_download(self, stage_mod, tmp_path, monkeypatch):
        """The gap the byte-pin left: bumping PROVIDER_VERSION without
        updating the hash passed every test and only failed at build time.
        Now an unpinned version fails immediately, and fails offline."""
        monkeypatch.setattr(stage_mod, "DOWNLOAD_CACHE", tmp_path)
        monkeypatch.setattr(stage_mod, "SOURCE_ZIP", tmp_path / "unused.zip")
        monkeypatch.setattr(stage_mod, "PROVIDER_VERSION", "99.99.99")
        monkeypatch.setattr(stage_mod, "PINNED_SOURCE_TREES", {"1.3.1": "0" * 64})

        def fail_if_called(*_a, **_k):
            pytest.fail("an unpinned version must not reach the network")

        monkeypatch.setattr(stage_mod.urllib.request, "urlretrieve", fail_if_called)

        with pytest.raises(RuntimeError, match="No pinned source digest"):
            stage_mod._download_source_archive()


class TestCommittedPin:
    """The committed constants must be self-consistent. Every test above
    monkeypatches the pin, so without this nothing checks the values that
    actually ship -- which is how the byte-pin could have gone stale against
    PROVIDER_VERSION without a single test noticing."""

    def test_the_committed_version_has_a_committed_pin(self, stage_mod):
        assert stage_mod.PROVIDER_VERSION in stage_mod.PINNED_SOURCE_TREES, (
            f"PROVIDER_VERSION is {stage_mod.PROVIDER_VERSION!r} but "
            f"PINNED_SOURCE_TREES only pins "
            f"{sorted(stage_mod.PINNED_SOURCE_TREES)} -- record the digest "
            f"with: python packaging/stage_pot_provider.py --print-tree-digest"
        )

    def test_every_committed_pin_is_a_real_sha256(self, stage_mod):
        for version, digest in stage_mod.PINNED_SOURCE_TREES.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), (
                f"the pin for {version} is not a SHA-256 digest: {digest!r} "
                f"-- a placeholder here means the build verifies nothing"
            )


class TestCliEntryPoint:

    def test_main_exits_zero_on_success_nonzero_on_failure(self, tmp_path):
        """Runs a *copy* of the script under tmp_path (not the real one in
        the repo) so HERE/STAGING_ROOT resolve there instead — this must
        never write into the real packaging/yt-dlp-plugins/ as a side
        effect of running the test suite."""
        import subprocess

        isolated_script = tmp_path / "stage_pot_provider.py"
        isolated_script.write_text(SCRIPT_PATH.read_text(encoding="utf-8"), encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(isolated_script)],
            capture_output=True, text=True, cwd=str(tmp_path), timeout=30,
        )
        # If the provider package and bundled Deno are present in this
        # isolated copy, exit 0; otherwise exit 1. Either way it must not
        # crash.
        assert result.returncode in (0, 1)
        assert "Traceback" not in result.stderr
