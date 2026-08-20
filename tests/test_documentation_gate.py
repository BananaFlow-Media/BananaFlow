from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_documentation", ROOT / "scripts" / "check_documentation.py"
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def test_documentation_gate_static_tree_is_green():
    assert mod.run() == []


def test_stable_release_detection_uses_version_source_of_truth():
    assert mod._is_stable() is True


def test_current_platform_support_wording_has_no_retired_status():
    assert mod.check_platform_support_language() == []


def test_provider_version_sources_agree():
    values = mod._provider_versions()
    assert len(values) >= 2
    assert len(set(values.values())) == 1


def test_ytdlp_compatibility_floor_and_reviewed_pin_agree():
    floors = mod._ytdlp_floor_sources()
    assert set(floors) == {
        "pyproject.toml",
        "core/youtube_doctor.py",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
    }
    assert len(set(floors.values())) == 1
    exact = mod._ytdlp_exact_release_pins()
    assert set(exact) == {"requirements.txt", "pyproject.toml dev extra"}
    assert len(set(exact.values())) == 1
    assert mod.check_ytdlp_consistency() == []


def test_ai_adapters_point_to_canonical_context():
    assert mod.check_ai_adapters() == []
